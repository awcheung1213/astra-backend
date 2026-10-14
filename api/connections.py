"""Site connect — how measuring becomes fixing: credentials are verified by
a LIVE read through the same adapter that will later write with them, then
encrypted and stored. Nothing is stored on a failed verify. A successful
connect closes the grant_access client task (the chase stops nagging). It
does NOT queue an audit: launch queues the first one and the weekly schedule
owns the rest (a connect during onboarding used to double the first audit —
e2e 2026-09-15, gap 6).

Auth rides the router-level guard (utils/authz.py) — these routes accept
secrets, which is why the guard had to land before any public deploy. The
Google callback lives on public_router: Google's browser redirect carries no
bearer token, so its HMAC-signed state IS the auth. WordPress one-tap's
callback lives there too, for the same reason — and so does the Microsoft
(Outlook mailbox) callback.

Mailboxes for outreach email (provider "gmail" / "outlook") ride the same
state + callback machinery: gmail is one more provider on the Google OAuth
app (send-only scope); outlook is a Microsoft identity-platform app. GET
email_sender reports which route an approved pitch would take."""

import base64
import hashlib
import hmac
import json
import re
import time
import uuid
from urllib.parse import quote, urlencode, urlsplit

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import RedirectResponse

import settings
from adapters import google_adapter
from adapters.cms.common import CmsWriteError
from adapters.google_adapter import GoogleReadError
from adapters.mail import outlook as outlook_mail
from adapters.mail.gmail import GMAIL_SCOPES, consented_mailbox
from objects.brand import PLATFORM_SHOPIFY, PLATFORM_WORDPRESS, Brand
from objects.connection import Connection
from objects.site import STATUS_CONNECTED, STATUS_DISCONNECTED, STATUS_ERROR, STATUS_PENDING, Site
from objects.task import STATUS_DONE, Task
from schemas.base_schemas import utc_iso
from schemas.connection_schemas import ConnectionsResponse, PickPropertyRequest, ProviderStatusResponse, SiteConnectRequest, SiteStatusResponse, WordPressAuthorizeRequest
from utils.logger import logger
from utils.pitch_mail import ROUTE_GMAIL, ROUTE_OUTLOOK, mail_return_url, owner_email_for, resolve_sender
from utils.token_crypto import decrypt_credentials, encrypt_credentials
from worker.tasks.changes import ADAPTER_BY_PLATFORM

router = APIRouter(prefix="/api/v1")
# registered WITHOUT the auth guard (main.py): Google's and WordPress's
# browser redirects carry no bearer token — the HMAC-signed state IS the auth
public_router = APIRouter(prefix="/api/v1")

REQUIRED_CREDENTIAL_KEYS = {
    PLATFORM_WORDPRESS: ("base_url", "username", "app_password"),
    # 2026+ custom-app model: client id/secret, exchanged for tokens by the adapter
    PLATFORM_SHOPIFY: ("shop_domain", "client_id", "client_secret"),
}


@router.get("/brands/{brand_id}/connections", response_model=ConnectionsResponse)
async def connections(brand_id: str) -> ConnectionsResponse:
    brand = await Brand.get(brand_id)
    site = await Site.any_for_brand(brand.id)
    return ConnectionsResponse.create(
        {
            "site": _site_status(site) if site else None,
            "providers": [ProviderStatusResponse.create({"provider": connection.provider, "status": connection.status, "connected_at": utc_iso(connection.connected_at)}) for connection in await Connection.for_brand(brand.id)],
        }
    )


@router.post("/brands/{brand_id}/site", response_model=SiteStatusResponse)
async def connect_site(brand_id: str, request: SiteConnectRequest) -> SiteStatusResponse:
    brand = await Brand.get(brand_id)
    if request.platform not in REQUIRED_CREDENTIAL_KEYS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"platform must be one of {sorted(REQUIRED_CREDENTIAL_KEYS)}")
    credentials = _normalized_credentials(request.platform, request.credentials)
    try:
        site = await connect_verified_site(brand.id, request.platform, credentials)
    except CmsWriteError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=verify_failure_detail(request.platform, error))
    return _site_status(site)


async def connect_verified_site(brand_id: str, platform: str, credentials: dict) -> Site:
    """The ONE verify-then-store path — manual connect and WordPress one-tap
    both land here with normalized credentials. Raises CmsWriteError when the
    live verify fails (after recording the error, unless a working
    connection is already on file)."""
    encrypted = encrypt_credentials(credentials)

    # verify with the SAME adapter that will later write through these
    # credentials — an unsaved probe row, so nothing persists on failure
    probe = Site(brand_id=brand_id, platform=platform, credentials_encrypted=encrypted)
    try:
        await ADAPTER_BY_PLATFORM[platform](probe).verify()  # type: ignore[attr-defined]
    except CmsWriteError:
        # a failed RE-connect must not clobber a working connection
        existing = await Site.any_for_brand(brand_id)
        if existing is None or existing.status != STATUS_CONNECTED:
            await Site.upsert_for_brand(brand_id, platform, STATUS_ERROR, credentials_encrypted=None)
        raise

    site = await Site.upsert_for_brand(brand_id, platform, STATUS_CONNECTED, credentials_encrypted=encrypted)
    await Brand.update(brand_id, platform=platform)

    # the connection IS what grant_access asked for — stop the daily chase
    grant_access_task = await Task.first_queued(brand_id, "grant_access")
    if grant_access_task is not None:
        await Task.update_status(grant_access_task.id, STATUS_DONE)
    return site


WORDPRESS_REJECTED_DETAIL = "WordPress didn't accept that. Use an application password, not your login password."


def verify_failure_detail(platform: str, error: CmsWriteError) -> str:
    """What the owner reads when verify fails: WordPress refusing the
    credentials (401/403) is almost always a login password pasted where an
    application password goes — say so plainly instead of the raw API text. Pure."""
    if platform == PLATFORM_WORDPRESS and error.status_code in (401, 403):
        return WORDPRESS_REJECTED_DETAIL
    return f"could not verify the connection: {error}"


@router.delete("/brands/{brand_id}/site", response_model=SiteStatusResponse)
async def disconnect_site(brand_id: str) -> SiteStatusResponse:
    brand = await Brand.get(brand_id)
    site = await Site.any_for_brand(brand.id)
    if site is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no site to disconnect")
    site = await Site.upsert_for_brand(brand.id, site.platform, STATUS_DISCONNECTED, credentials_encrypted=None)
    return _site_status(site)


def _site_status(site: Site) -> SiteStatusResponse:
    return SiteStatusResponse.create({"platform": site.platform, "status": site.status, "connected_at": utc_iso(site.connected_at)})


##############################
# GOOGLE OAUTH (gsc + ga4)
##############################
# Ported from geoviz-backend/api/integrations.py. The callback arrives from
# the USER'S BROWSER with no bearer token — even after the auth port it stays
# public, trusting only its HMAC-signed state (brand, provider, nonce,
# expiry — minted by authorize, signed with TOKEN_ENCRYPTION_KEY).

GOOGLE_PROVIDER_SCOPES = {
    "gsc": "https://www.googleapis.com/auth/webmasters.readonly",
    "ga4": "https://www.googleapis.com/auth/analytics.readonly",
    # outreach email: send-only (never a read scope) + openid email, which
    # names the authorized address (gmail.send can't call users.getProfile)
    ROUTE_GMAIL: GMAIL_SCOPES,
}
# the providers with a property to pick (gmail has none)
PROPERTY_PROVIDERS = ("gsc", "ga4")
# generous: a first-time consent can involve account choosers, permission
# review, even a mid-flow detour — an expired state costs a full retry
STATE_TTL_SECONDS = 1800


def _sign(payload: bytes) -> str:
    return hmac.new(settings.TOKEN_ENCRYPTION_KEY.encode(), payload, hashlib.sha256).hexdigest()


def mint_state(brand_id: str, provider: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"brand_id": brand_id, "provider": provider, "nonce": str(uuid.uuid4()), "exp": int(time.time()) + STATE_TTL_SECONDS}).encode())
    return f"{payload.decode()}.{_sign(payload)}"


def verify_state(state: str, ignore_exp: bool = False) -> dict | None:
    """ignore_exp lets the callback distinguish a stale-but-authentic state
    (user parked on the Google screen too long -> retry) from a tampered one."""
    try:
        payload, signature = state.rsplit(".", 1)
        if not hmac.compare_digest(_sign(payload.encode()), signature):
            return None
        decoded = json.loads(base64.urlsafe_b64decode(payload.encode()))
        if not ignore_exp and decoded.get("exp", 0) < time.time():
            return None
        return decoded
    except Exception:
        return None


def _website_domain(website: str) -> str:
    domain = (website or "").lower().strip()
    for prefix in ("https://", "http://"):
        domain = domain.removeprefix(prefix)
    return domain.removeprefix("www.").split("/", 1)[0]


def match_gsc_property(properties: list[str], website_url: str) -> str | None:
    """Auto-select when exactly one GSC property matches the brand's website:
    `sc-domain:example.com` covers all subdomains/protocols; URL-prefix
    properties match on host."""
    domain = _website_domain(website_url)
    if not domain:
        return None
    matches = []
    for property_url in properties:
        if property_url.startswith("sc-domain:"):
            property_domain = property_url.removeprefix("sc-domain:").lower()
            if domain == property_domain or domain.endswith(f".{property_domain}"):
                matches.append(property_url)
        elif _website_domain(property_url) == domain:
            matches.append(property_url)
    return matches[0] if len(matches) == 1 else None


def google_authorize_url(provider: str, state: str, login_hint: str | None = None) -> str:
    """The Google consent URL for one provider. access_type=offline +
    prompt=consent because Google only returns a refresh token on a forced
    consent; gmail adds include_granted_scopes + the owner's address as
    login_hint (one less account chooser). Pure."""
    params = {
        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URL,
        "response_type": "code",
        "scope": GOOGLE_PROVIDER_SCOPES[provider],
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    if provider == ROUTE_GMAIL:
        params["include_granted_scopes"] = "true"
        if login_hint:
            params["login_hint"] = login_hint
    return f"{google_adapter.GOOGLE_AUTH_URL}?{urlencode(params, quote_via=quote)}"


@router.post("/brands/{brand_id}/connections/{provider}/authorize")
async def google_authorize(brand_id: str, provider: str) -> dict:
    """Returns the consent URL; the SPA navigates to it directly. Google for
    gsc/ga4/gmail; Microsoft for outlook (dispatched here because this route
    pattern would otherwise swallow it)."""
    brand = await Brand.get(brand_id)
    if provider == ROUTE_OUTLOOK:
        if not outlook_mail.microsoft_configured():
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Outlook sending isn't set up yet.")
        return {"url": outlook_mail.outlook_authorize_url(mint_state(brand.id, ROUTE_OUTLOOK), await owner_email_for(brand))}
    if provider not in GOOGLE_PROVIDER_SCOPES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"provider must be one of {sorted([*GOOGLE_PROVIDER_SCOPES, ROUTE_OUTLOOK])}")
    if not settings.GOOGLE_OAUTH_CLIENT_ID:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Google OAuth is not configured")
    login_hint = await owner_email_for(brand) if provider == ROUTE_GMAIL else None
    return {"url": google_authorize_url(provider, mint_state(brand.id, provider), login_hint)}


@public_router.get("/connections/google/callback")
async def google_callback(state: str = "", code: str = "", error: str = "") -> RedirectResponse:
    """Google's browser redirect — the signed state is the auth. Exchanges
    the code; GSC auto-connects when exactly one property matches the brand's
    website, otherwise (and always for GA4) the row lands pending for a
    property pick. Bounces back to the dashboard with the outcome."""

    def back(provider: str, result: str) -> RedirectResponse:
        # gmail returns to the tasks screen, where the pitch he was sending waits
        url = mail_return_url(ROUTE_GMAIL, result) if provider == ROUTE_GMAIL else f"{settings.DASHBOARD_URL}/?screen=connect&google={provider}&result={result}"
        return RedirectResponse(url, status_code=status.HTTP_302_FOUND)

    decoded = verify_state(state)
    if decoded is None:
        stale = verify_state(state, ignore_exp=True)
        if stale is not None:
            logger.info(f"brand {stale['brand_id']}: google callback with expired state — asking for a retry")
            return back(stale.get("provider", "gsc"), "expired")
        logger.error("google callback with invalid state — rejected")
        return back("gsc", "error")
    brand_id, provider = decoded["brand_id"], decoded["provider"]
    if provider not in GOOGLE_PROVIDER_SCOPES:
        logger.error(f"brand {brand_id}: google callback with a {provider} state — rejected")
        return back("gsc", "error")
    if error:
        # access_denied is user/app-policy specific (declined consent, a
        # Workspace admin block, or a Testing-status app missing this test
        # user) — surface it distinctly so support doesn't need server logs
        logger.info(f"brand {brand_id}: google consent declined ({error})")
        return back(provider, "denied" if error == "access_denied" else "error")

    try:
        tokens = await google_adapter.exchange_google_code(code)
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            # can happen when a prior grant exists; prompt=consent makes this rare
            logger.error(f"brand {brand_id}: google returned no refresh token")
            return back(provider, "error")

        if provider == ROUTE_GMAIL:
            mailbox = consented_mailbox(tokens)
            if mailbox is None:
                # send permission unticked on the granular consent screen (or no verified address)
                logger.info(f"brand {brand_id}: gmail consent without send permission")
                return back(provider, "denied")
            await Connection.upsert(brand_id, ROUTE_GMAIL, STATUS_CONNECTED, encrypt_credentials({"refresh_token": refresh_token, "email": mailbox}), meta={"email": mailbox})
            logger.info(f"brand {brand_id}: gmail connected for outreach email")
            return back(provider, "connected")

        if provider == "gsc":
            brand = await Brand.get(brand_id)
            properties = await google_adapter.list_gsc_properties(tokens["access_token"])
            matched = match_gsc_property(properties, brand.website_url)
            if matched:
                await Connection.upsert(brand_id, "gsc", STATUS_CONNECTED, encrypt_credentials({"refresh_token": refresh_token, "site_url": matched}))
                logger.info(f"brand {brand_id}: GSC connected to {matched}")
                return back(provider, "connected")
            await Connection.upsert(brand_id, "gsc", STATUS_PENDING, encrypt_credentials({"refresh_token": refresh_token}))
            logger.info(f"brand {brand_id}: GSC consented, {len(properties)} properties, none auto-matched")
            return back(provider, "pick_property")

        # ga4: no auto-match is possible — always a property pick
        await Connection.upsert(brand_id, "ga4", STATUS_PENDING, encrypt_credentials({"refresh_token": refresh_token}))
        return back(provider, "pick_property")
    except Exception as exc:
        logger.error(f"brand {brand_id}: google callback failed: {exc}")
        return back(provider, "error")


@router.get("/brands/{brand_id}/connections/{provider}/properties")
async def google_properties(brand_id: str, provider: str) -> dict:
    """The live property list for a consented-but-unmatched connection —
    what the pick step renders."""
    brand = await Brand.get(brand_id)
    connection = await _google_connection(brand.id, provider)
    access_token = await google_adapter.fetch_google_access_token_standalone(decrypt_credentials(connection.credentials_encrypted or "")["refresh_token"])
    try:
        properties = await google_adapter.list_gsc_properties(access_token) if provider == "gsc" else await google_adapter.list_ga4_properties(access_token)
    except GoogleReadError as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error))
    return {"provider": provider, "properties": properties}


@router.post("/brands/{brand_id}/connections/{provider}/property", response_model=ProviderStatusResponse)
async def google_pick_property(brand_id: str, provider: str, request: PickPropertyRequest) -> ProviderStatusResponse:
    """Resolve a pending consent by choosing one of the account's properties
    (validated against the LIVE list, so a stale or foreign pick can't land)."""
    brand = await Brand.get(brand_id)
    connection = await _google_connection(brand.id, provider)
    credentials = decrypt_credentials(connection.credentials_encrypted or "")
    access_token = await google_adapter.fetch_google_access_token_standalone(credentials["refresh_token"])
    if provider == "gsc":
        if request.property not in await google_adapter.list_gsc_properties(access_token):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="that property is not accessible to the connected Google account")
        credentials["site_url"] = request.property
    else:
        if request.property not in {ga4_property["property_id"] for ga4_property in await google_adapter.list_ga4_properties(access_token)}:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="that property is not accessible to the connected Google account")
        credentials["property_id"] = request.property
    connection = await Connection.upsert(brand.id, provider, STATUS_CONNECTED, encrypt_credentials(credentials))
    return ProviderStatusResponse.create({"provider": provider, "status": connection.status, "connected_at": utc_iso(connection.connected_at)})


@router.delete("/brands/{brand_id}/connections/{provider}", response_model=ProviderStatusResponse)
async def google_disconnect(brand_id: str, provider: str) -> ProviderStatusResponse:
    """Revoke at Google (best effort) and forget the connection. Mailboxes
    (gmail/outlook) are forgotten WITHOUT a provider revoke: a Google revoke
    kills the whole grant for our app — with include_granted_scopes that
    would take GSC/GA4 down with it — and Microsoft has no per-app revoke."""
    brand = await Brand.get(brand_id)
    if provider not in GOOGLE_PROVIDER_SCOPES and provider != ROUTE_OUTLOOK:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"provider must be one of {sorted([*GOOGLE_PROVIDER_SCOPES, ROUTE_OUTLOOK])}")
    connection = await Connection.get_for_brand_provider(brand.id, provider)
    if connection is not None and connection.credentials_encrypted and provider in PROPERTY_PROVIDERS:
        try:
            await google_adapter.revoke_google_token(decrypt_credentials(connection.credentials_encrypted)["refresh_token"])
        except Exception as exc:
            logger.error(f"brand {brand.id}: google revoke failed (deleting anyway): {exc}")
    await Connection.delete_for_brand_provider(brand.id, provider)
    return ProviderStatusResponse.create({"provider": provider, "status": "disconnected", "connected_at": None})


async def _google_connection(brand_id: str, provider: str) -> Connection:
    if provider not in PROPERTY_PROVIDERS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"provider must be one of {sorted(PROPERTY_PROVIDERS)}")
    connection = await Connection.get_for_brand_provider(brand_id, provider)
    if connection is None or not connection.credentials_encrypted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no {provider} consent on file — connect first")
    return connection


##############################
# OUTREACH EMAIL (which mailbox a pitch goes out from)
##############################


@router.get("/brands/{brand_id}/email_sender")
async def email_sender(brand_id: str) -> dict:
    """How an approved pitch would go out right now: {route: gmail|outlook|
    evergreen|none, connected, from_address, from_name, reply_to,
    owner_email}. gmail/outlook unconnected = the frontend's connect step;
    none = copy-paste. See utils/pitch_mail.py."""
    return await resolve_sender(await Brand.get(brand_id))


@public_router.get("/connections/microsoft/callback")
async def microsoft_callback(state: str = "", code: str = "", error: str = "") -> RedirectResponse:
    """Microsoft's browser redirect after the Outlook consent — the signed
    state is the auth (same mint/verify as Google's). Exchanges the code,
    names the mailbox (Graph /me), stores the encrypted refresh token, and
    bounces to the tasks screen with the outcome."""

    def back(result: str) -> RedirectResponse:
        return RedirectResponse(mail_return_url(ROUTE_OUTLOOK, result), status_code=status.HTTP_302_FOUND)

    decoded = verify_state(state)
    if decoded is None or decoded.get("provider") != ROUTE_OUTLOOK:
        stale = verify_state(state, ignore_exp=True)
        if decoded is None and stale is not None and stale.get("provider") == ROUTE_OUTLOOK:
            logger.info(f"brand {stale['brand_id']}: microsoft callback with expired state — asking for a retry")
            return back("expired")
        logger.error("microsoft callback with invalid state — rejected")
        return back("error")
    brand_id = decoded["brand_id"]
    if error:
        logger.info(f"brand {brand_id}: microsoft consent declined ({error})")
        return back("denied" if error in ("access_denied", "consent_required") else "error")

    try:
        tokens = await outlook_mail.exchange_microsoft_code(code)
        refresh_token = tokens.get("refresh_token")
        if not refresh_token or not tokens.get("access_token"):
            logger.error(f"brand {brand_id}: microsoft returned no refresh token")
            return back("error")
        mailbox = await outlook_mail.microsoft_account_email(tokens["access_token"], tokens.get("id_token"))
        if not mailbox:
            logger.error(f"brand {brand_id}: microsoft consent named no mailbox")
            return back("error")
        await Connection.upsert(brand_id, ROUTE_OUTLOOK, STATUS_CONNECTED, encrypt_credentials({"refresh_token": refresh_token, "email": mailbox}), meta={"email": mailbox})
    except Exception as exc:
        logger.error(f"brand {brand_id}: microsoft callback failed: {type(exc).__name__}")
        return back("error")
    logger.info(f"brand {brand_id}: outlook connected for outreach email")
    return back("connected")


##############################
# WORDPRESS ONE-TAP (application-password authorization screen)
##############################
# WordPress core's own approval screen mints the application password:
# {site}/wp-admin/authorize-application.php shows "Evergreen wants access",
# the owner taps Approve, and WordPress redirects the browser to success_url
# with site_url + user_login + password in the query (or to reject_url with
# success=false). No password to find or paste. Like Google's, the callback
# is PUBLIC and its HMAC-signed state (mint_state/verify_state, provider
# "wordpress") is the auth; the credentials then take the SAME
# verify-then-store path as a manual connect. The password is never logged
# (the access log + request log redact it: middleware/logging_middleware.py).

WORDPRESS_APP_NAME = "Evergreen"
WORDPRESS_APP_ID = "64a3f977-cf3b-4db4-a43b-31997b82b6ca"  # stable: WordPress groups the owner's app passwords by it
WORDPRESS_RESULTS = ("connected", "rejected", "failed", "expired")


def normalize_wordpress_site_url(site_url: str, fallback: str) -> str:
    """The WordPress install's base URL from what the owner typed (blank ->
    the brand's website): scheme defaulted to https, query/fragment and any
    pasted /wp-admin or /wp-login.php tail dropped, no trailing slash. '' when
    there is nothing to go on. Pure."""
    raw = (site_url or "").strip() or (fallback or "").strip()
    if not raw:
        return ""
    if not re.match(r"^https?://", raw, flags=re.IGNORECASE):
        raw = f"https://{raw}"
    parsed = urlsplit(raw)
    path = re.split(r"/wp-(?:admin|login\.php)\b", parsed.path, maxsplit=1)[0].rstrip("/")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"


def wordpress_authorize_url(site_url: str, state: str, callback_url: str) -> str:
    """WordPress's authorize-application screen for this site, approving or
    rejecting back to the callback with the signed state. Pure."""
    callback = f"{callback_url}?{urlencode({'state': state})}"
    params = {"app_name": WORDPRESS_APP_NAME, "app_id": WORDPRESS_APP_ID, "success_url": callback, "reject_url": callback}
    return f"{site_url}/wp-admin/authorize-application.php?{urlencode(params, quote_via=quote)}"


def wordpress_return_url(result: str) -> str:
    """Where the callback bounces the browser: the dashboard's Connect screen
    with the outcome (mirrors Google's back()). Pure."""
    return f"{settings.DASHBOARD_URL}/?screen=connect&wordpress=1&result={result}"


def wordpress_state_brand(state: str) -> tuple[str, str | None]:
    """("ok", brand_id) for a valid WordPress state; ("expired", brand_id)
    for an authentic one past its TTL; ("invalid", None) for anything else —
    tampered, garbage, or a Google state replayed here. Pure."""
    decoded = verify_state(state)
    if decoded is not None and decoded.get("provider") == PLATFORM_WORDPRESS:
        return "ok", decoded["brand_id"]
    stale = verify_state(state, ignore_exp=True)
    if stale is not None and stale.get("provider") == PLATFORM_WORDPRESS:
        return "expired", stale["brand_id"]
    return "invalid", None


@router.post("/brands/{brand_id}/site/wordpress/authorize")
async def wordpress_authorize(brand_id: str, request: WordPressAuthorizeRequest | None = None) -> dict:
    """Returns WordPress's approval-screen URL; the SPA navigates to it.
    A blank site_url means the brand's own website."""
    brand = await Brand.get(brand_id)
    site_url = normalize_wordpress_site_url((request or WordPressAuthorizeRequest()).site_url, brand.website_url)
    if not site_url:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="site_url is required")
    return {"url": wordpress_authorize_url(site_url, mint_state(brand.id, PLATFORM_WORDPRESS), settings.WORDPRESS_CONNECT_CALLBACK_URL)}


@public_router.get("/connections/wordpress/callback")
async def wordpress_callback(state: str = "", success: str = "", site_url: str = "", user_login: str = "", password: str = "") -> RedirectResponse:
    """WordPress's browser redirect after Approve/Reject — the signed state is
    the auth. Approved credentials run the manual connect's verify-then-store
    path; the browser lands back on the Connect screen with the outcome."""

    def back(result: str) -> RedirectResponse:
        return RedirectResponse(wordpress_return_url(result), status_code=status.HTTP_302_FOUND)

    state_status, brand_id = wordpress_state_brand(state)
    if state_status == "expired":
        logger.info(f"brand {brand_id}: wordpress callback with expired state — asking for a retry")
        return back("expired")
    if state_status != "ok" or brand_id is None:
        logger.error("wordpress callback with invalid state — rejected")
        return back("failed")
    if success == "false" or not (site_url.strip() and user_login.strip() and password.strip()):
        logger.info(f"brand {brand_id}: wordpress approval declined or incomplete")
        return back("rejected")

    credentials = _normalized_credentials(PLATFORM_WORDPRESS, {"base_url": site_url, "username": user_login, "app_password": password})
    try:
        await connect_verified_site(brand_id, PLATFORM_WORDPRESS, credentials)
    except CmsWriteError as error:
        logger.error(f"brand {brand_id}: wordpress one-tap verify failed: {error}")
        return back("failed")
    except Exception as exc:
        logger.error(f"brand {brand_id}: wordpress callback failed: {type(exc).__name__}")
        return back("failed")
    logger.info(f"brand {brand_id}: wordpress connected via one-tap ({credentials['base_url']})")
    return back("connected")


def _normalized_credentials(platform: str, credentials: dict) -> dict:
    cleaned = {key: str(credentials.get(key) or "").strip() for key in REQUIRED_CREDENTIAL_KEYS[platform]}
    missing = [key for key, value in cleaned.items() if not value]
    if missing:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"missing credential field(s): {', '.join(missing)}")
    if platform == PLATFORM_WORDPRESS:
        base_url = cleaned["base_url"].rstrip("/")
        cleaned["base_url"] = base_url if base_url.startswith("http") else f"https://{base_url}"
    if platform == PLATFORM_SHOPIFY:
        cleaned["shop_domain"] = re.sub(r"^https?://", "", cleaned["shop_domain"]).strip("/")
    return cleaned
