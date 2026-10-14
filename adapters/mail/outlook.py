"""Outlook send adapter — an approved pitch goes out from the owner's OWN
Outlook.com / Microsoft 365 mailbox via Microsoft Graph POST /me/sendMail
(saveToSentItems true, so it sits in his Sent Items like any email he wrote).

OAuth: Microsoft identity platform, the /common authority (personal AND
work/school accounts), delegated scopes Mail.Send + offline_access, plus
`openid email User.Read` so the callback can name the authorized address
(Graph /me, falling back to the id_token's claims). No mail READ scope is
ever requested. Encrypted credentials on the connection row (provider
"outlook"): {"refresh_token", "email"}. Microsoft may rotate the refresh
token on every redemption — send_outlook returns the new one so the caller
can store it.

Every send records a $0 cost_event (adapter "mail_outlook")."""

from urllib.parse import quote, urlencode

import httpx

import settings
from adapters.mail.common import MailSendError, header_safe, jwt_claims_unverified
from objects.cost_event import CostEvent

MICROSOFT_AUTHORITY = "https://login.microsoftonline.com/common/oauth2/v2.0"
MICROSOFT_AUTH_URL = f"{MICROSOFT_AUTHORITY}/authorize"
MICROSOFT_TOKEN_URL = f"{MICROSOFT_AUTHORITY}/token"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
OUTLOOK_SCOPES = "openid email offline_access https://graph.microsoft.com/User.Read https://graph.microsoft.com/Mail.Send"
OUTLOOK_OPEN_URL = "https://outlook.live.com/mail/0/sentitems"
TIMEOUT_SECONDS = 30.0

RECONNECT_PLAIN = "Outlook didn't accept it — reconnect and try again."
TRY_AGAIN_PLAIN = "Outlook had a problem sending it — try again in a few minutes."
MAYBE_SENT_PLAIN = "We're not sure Outlook sent it — check your Sent folder before sending again."


class MicrosoftOAuthError(Exception):
    """The consent half of the OAuth dance failed (callback-side)."""


def microsoft_configured() -> bool:
    return bool(settings.MICROSOFT_OAUTH_CLIENT_ID and settings.MICROSOFT_OAUTH_CLIENT_SECRET)


def outlook_authorize_url(state: str, login_hint: str | None) -> str:
    """The Microsoft consent URL. prompt=select_account so an owner signed in
    to the wrong account can pick the right one. Pure."""
    params = {
        "client_id": settings.MICROSOFT_OAUTH_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": settings.MICROSOFT_OAUTH_REDIRECT_URL,
        "response_mode": "query",
        "scope": OUTLOOK_SCOPES,
        "prompt": "select_account",
        "state": state,
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{MICROSOFT_AUTH_URL}?{urlencode(params, quote_via=quote)}"


async def exchange_microsoft_code(code: str) -> dict:
    """Authorization code -> token payload (access_token, refresh_token, id_token)."""
    if not microsoft_configured():
        raise MicrosoftOAuthError("MICROSOFT_OAUTH_CLIENT_ID/SECRET not configured")
    data = {
        "client_id": settings.MICROSOFT_OAUTH_CLIENT_ID,
        "client_secret": settings.MICROSOFT_OAUTH_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": settings.MICROSOFT_OAUTH_REDIRECT_URL,
        "scope": OUTLOOK_SCOPES,
    }
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        try:
            response = await client.post(MICROSOFT_TOKEN_URL, data=data)
        except httpx.HTTPError as error:
            raise MicrosoftOAuthError(f"microsoft code exchange failed: {type(error).__name__}") from error
    if response.status_code >= 400:
        raise MicrosoftOAuthError(f"microsoft code exchange -> {response.status_code}")
    return response.json()


def email_from_microsoft_claims(claims: dict) -> str | None:
    """The account's address from id_token claims: `email`, else a
    `preferred_username` that looks like an address. Pure."""
    for key in ("email", "preferred_username"):
        value = str(claims.get(key) or "").strip().lower()
        if "@" in value:
            return value
    return None


async def microsoft_account_email(access_token: str, id_token: str | None) -> str | None:
    """The mailbox the grant sends as: Graph /me (mail, else userPrincipalName),
    falling back to the id_token's claims. A free read — no cost event."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.get(f"{GRAPH_BASE}/me", params={"$select": "mail,userPrincipalName"}, headers={"Authorization": f"Bearer {access_token}"})
        if response.status_code == 200:
            profile = response.json()
            address = str(profile.get("mail") or profile.get("userPrincipalName") or "").strip().lower()
            if "@" in address:
                return address
    except httpx.HTTPError:
        pass
    return email_from_microsoft_claims(jwt_claims_unverified(id_token))


def graph_send_payload(to_address: str, to_name: str | None, subject: str, body: str) -> dict:
    """The /me/sendMail body: plain text, one recipient, kept in Sent Items. Pure."""
    recipient: dict = {"address": to_address}
    if header_safe(to_name):
        recipient["name"] = header_safe(to_name)
    return {
        "message": {"subject": header_safe(subject), "body": {"contentType": "Text", "content": body or ""}, "toRecipients": [{"emailAddress": recipient}]},
        "saveToSentItems": True,
    }


async def _refresh(client: httpx.AsyncClient, refresh_token: str) -> dict:
    if not microsoft_configured():
        raise MailSendError("microsoft oauth not configured", TRY_AGAIN_PLAIN)
    data = {"client_id": settings.MICROSOFT_OAUTH_CLIENT_ID, "client_secret": settings.MICROSOFT_OAUTH_CLIENT_SECRET, "refresh_token": refresh_token, "grant_type": "refresh_token", "scope": OUTLOOK_SCOPES}
    try:
        response = await client.post(MICROSOFT_TOKEN_URL, data=data)
    except httpx.HTTPError as error:
        raise MailSendError(f"microsoft token refresh failed: {type(error).__name__}", TRY_AGAIN_PLAIN) from error
    if response.status_code >= 400:
        revoked = response.status_code in (400, 401) and ("invalid_grant" in response.text or "interaction_required" in response.text)
        raise MailSendError(f"microsoft token refresh -> {response.status_code}", RECONNECT_PLAIN if revoked else TRY_AGAIN_PLAIN, reconnect=revoked)
    return response.json()


async def send_outlook(refresh_token: str, payload: dict, organization_id: str | None, brand_id: str | None, task_id: str | None) -> str | None:
    """Send one message. Graph's sendMail answers 202 with no message id.
    Returns the ROTATED refresh token when Microsoft issued one (the caller
    stores it), else None. Raises MailSendError."""
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        tokens = await _refresh(client, refresh_token)
        try:
            response = await client.post(f"{GRAPH_BASE}/me/sendMail", json=payload, headers={"Authorization": f"Bearer {tokens['access_token']}"})
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            raise MailSendError(f"graph sendMail could not connect: {type(error).__name__}", TRY_AGAIN_PLAIN) from error
        except httpx.HTTPError as error:
            raise MailSendError(f"graph sendMail interrupted: {type(error).__name__}", MAYBE_SENT_PLAIN, maybe_sent=True) from error
    if response.status_code == 429:
        raise MailSendError("graph sendMail -> 429 (throttled)", TRY_AGAIN_PLAIN)
    if response.status_code in (401, 403):
        raise MailSendError(f"graph sendMail -> {response.status_code}", RECONNECT_PLAIN, reconnect=True)
    if response.status_code >= 400:
        raise MailSendError(f"graph sendMail -> {response.status_code}: {response.text[:200]}", RECONNECT_PLAIN if response.status_code < 500 else TRY_AGAIN_PLAIN)
    await CostEvent.create(adapter="mail_outlook", operation="send_pitch", organization_id=organization_id, brand_id=brand_id, task_id=task_id, usd=0.0, own_transaction=True)
    rotated = tokens.get("refresh_token")
    return rotated if rotated and rotated != refresh_token else None
