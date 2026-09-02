"""Site connect — how measuring becomes fixing: credentials are verified by
a LIVE read through the same adapter that will later write with them, then
encrypted and stored. Nothing is stored on a failed verify. A successful
connect closes the grant_access client task (the chase stops nagging) and
fires the site audit README promises "at site connect".

⚠ NO AUTH YET (repo-wide) — these routes accept secrets, so the auth port
must land before this deploys anywhere public. Local dev only until then."""

import base64
import hashlib
import hmac
import json
import re
import time
import uuid
from urllib.parse import quote, urlencode

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import RedirectResponse

import settings
from adapters import google_adapter
from adapters.cms.common import CmsWriteError
from adapters.google_adapter import GoogleReadError
from objects.brand import PLATFORM_SHOPIFY, PLATFORM_WORDPRESS, Brand
from objects.connection import Connection
from objects.site import STATUS_CONNECTED, STATUS_DISCONNECTED, STATUS_ERROR, STATUS_PENDING, Site
from objects.task import STATUS_DONE, Task
from schemas.connection_schemas import ConnectionsResponse, PickPropertyRequest, ProviderStatusResponse, SiteConnectRequest, SiteStatusResponse
from utils.logger import logger
from utils.task_queue import create_task
from utils.token_crypto import decrypt_credentials, encrypt_credentials
from worker.tasks.changes import ADAPTER_BY_PLATFORM

router = APIRouter(prefix="/api/v1")

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
            "providers": [ProviderStatusResponse.create({"provider": connection.provider, "status": connection.status, "connected_at": connection.connected_at.isoformat() if connection.connected_at else None}) for connection in await Connection.for_brand(brand.id)],
        }
    )


@router.post("/brands/{brand_id}/site", response_model=SiteStatusResponse)
async def connect_site(brand_id: str, request: SiteConnectRequest) -> SiteStatusResponse:
    brand = await Brand.get(brand_id)
    if request.platform not in REQUIRED_CREDENTIAL_KEYS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"platform must be one of {sorted(REQUIRED_CREDENTIAL_KEYS)}")
    credentials = _normalized_credentials(request.platform, request.credentials)
    encrypted = encrypt_credentials(credentials)

    # verify with the SAME adapter that will later write through these
    # credentials — an unsaved probe row, so nothing persists on failure
    probe = Site(brand_id=brand.id, platform=request.platform, credentials_encrypted=encrypted)
    try:
        await ADAPTER_BY_PLATFORM[request.platform](probe).verify()  # type: ignore[attr-defined]
    except CmsWriteError as error:
        # a failed RE-connect must not clobber a working connection
        existing = await Site.any_for_brand(brand.id)
        if existing is None or existing.status != STATUS_CONNECTED:
            await Site.upsert_for_brand(brand.id, request.platform, STATUS_ERROR, credentials_encrypted=None)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"could not verify the connection: {error}")

    site = await Site.upsert_for_brand(brand.id, request.platform, STATUS_CONNECTED, credentials_encrypted=encrypted)
    await Brand.update(brand.id, platform=request.platform)

    # the connection IS what grant_access asked for — stop the daily chase
    grant_access_task = await Task.first_queued(brand.id, "grant_access")
    if grant_access_task is not None:
        await Task.update_status(grant_access_task.id, STATUS_DONE)

    # README: the audit "fires at site connect, then weekly" — this is the
    # connect half (reconnects re-audit on purpose; the crawl is cheap)
    await create_task(brand_id=brand.id, name="site_audit", queue="audit")
    return _site_status(site)


@router.delete("/brands/{brand_id}/site", response_model=SiteStatusResponse)
async def disconnect_site(brand_id: str) -> SiteStatusResponse:
    brand = await Brand.get(brand_id)
    site = await Site.any_for_brand(brand.id)
    if site is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no site to disconnect")
    site = await Site.upsert_for_brand(brand.id, site.platform, STATUS_DISCONNECTED, credentials_encrypted=None)
    return _site_status(site)


def _site_status(site: Site) -> SiteStatusResponse:
    return SiteStatusResponse.create({"platform": site.platform, "status": site.status, "connected_at": site.connected_at.isoformat() if site.connected_at else None})


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
}
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


@router.post("/brands/{brand_id}/connections/{provider}/authorize")
async def google_authorize(brand_id: str, provider: str) -> dict:
    """Returns the Google consent URL; the SPA navigates to it directly.
    access_type=offline + prompt=consent because Google only returns a
    refresh token on a forced consent."""
    brand = await Brand.get(brand_id)
    if provider not in GOOGLE_PROVIDER_SCOPES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"provider must be one of {sorted(GOOGLE_PROVIDER_SCOPES)}")
    if not settings.GOOGLE_OAUTH_CLIENT_ID:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Google OAuth is not configured")
    params = {
        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URL,
        "response_type": "code",
        "scope": GOOGLE_PROVIDER_SCOPES[provider],
        "access_type": "offline",
        "prompt": "consent",
        "state": mint_state(brand.id, provider),
    }
    return {"url": f"{google_adapter.GOOGLE_AUTH_URL}?{urlencode(params, quote_via=quote)}"}


@router.get("/connections/google/callback")
async def google_callback(state: str = "", code: str = "", error: str = "") -> RedirectResponse:
    """Google's browser redirect — the signed state is the auth. Exchanges
    the code; GSC auto-connects when exactly one property matches the brand's
    website, otherwise (and always for GA4) the row lands pending for a
    property pick. Bounces back to the dashboard with the outcome."""

    def back(provider: str, result: str) -> RedirectResponse:
        return RedirectResponse(f"{settings.DASHBOARD_URL}/?screen=connect&google={provider}&result={result}", status_code=status.HTTP_302_FOUND)

    decoded = verify_state(state)
    if decoded is None:
        stale = verify_state(state, ignore_exp=True)
        if stale is not None:
            logger.info(f"brand {stale['brand_id']}: google callback with expired state — asking for a retry")
            return back(stale.get("provider", "gsc"), "expired")
        logger.error("google callback with invalid state — rejected")
        return back("gsc", "error")
    brand_id, provider = decoded["brand_id"], decoded["provider"]
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
    return ProviderStatusResponse.create({"provider": provider, "status": connection.status, "connected_at": connection.connected_at.isoformat() if connection.connected_at else None})


@router.delete("/brands/{brand_id}/connections/{provider}", response_model=ProviderStatusResponse)
async def google_disconnect(brand_id: str, provider: str) -> ProviderStatusResponse:
    """Revoke at Google (best effort) and forget the connection."""
    brand = await Brand.get(brand_id)
    if provider not in GOOGLE_PROVIDER_SCOPES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"provider must be one of {sorted(GOOGLE_PROVIDER_SCOPES)}")
    connection = await Connection.get_for_brand_provider(brand.id, provider)
    if connection is not None and connection.credentials_encrypted:
        try:
            await google_adapter.revoke_google_token(decrypt_credentials(connection.credentials_encrypted)["refresh_token"])
        except Exception as exc:
            logger.error(f"brand {brand.id}: google revoke failed (deleting anyway): {exc}")
    await Connection.delete_for_brand_provider(brand.id, provider)
    return ProviderStatusResponse.create({"provider": provider, "status": "disconnected", "connected_at": None})


async def _google_connection(brand_id: str, provider: str) -> Connection:
    if provider not in GOOGLE_PROVIDER_SCOPES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"provider must be one of {sorted(GOOGLE_PROVIDER_SCOPES)}")
    connection = await Connection.get_for_brand_provider(brand_id, provider)
    if connection is None or not connection.credentials_encrypted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no {provider} consent on file — connect first")
    return connection


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
