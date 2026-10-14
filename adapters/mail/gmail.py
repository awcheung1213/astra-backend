"""Gmail send adapter — an approved pitch goes out from the owner's OWN Gmail
/ Google Workspace mailbox via users.messages.send (raw RFC 2822, base64url).

Scope: gmail.send ONLY (sensitive, send-only — it cannot read the mailbox)
plus `openid email` so the consent's id_token names the authorized address
(gmail.send alone does not permit users.getProfile). The refresh token comes
from the shared Google OAuth app (api/connections.py, provider "gmail").
Encrypted credentials on the connection row: {"refresh_token", "email"}.

Every send records a $0 cost_event (adapter "mail_gmail") — the ledger
counts sends even when they are free."""

import httpx

import settings
from adapters.google_adapter import GOOGLE_TOKEN_URL
from adapters.mail.common import MailSendError, base64url, jwt_claims_unverified
from objects.cost_event import CostEvent

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_SCOPES = f"openid email {GMAIL_SEND_SCOPE}"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
GMAIL_OPEN_URL = "https://mail.google.com/mail/u/0/#sent"
SEND_TIMEOUT_SECONDS = 30.0

RECONNECT_PLAIN = "Gmail didn't accept it — reconnect and try again."
TRY_AGAIN_PLAIN = "Gmail had a problem sending it — try again in a few minutes."
MAYBE_SENT_PLAIN = "We're not sure Gmail sent it — check your Sent folder before sending again."


def consented_mailbox(tokens: dict) -> str | None:
    """The authorized Gmail address from a code-exchange payload — None when
    the owner unticked "send email on your behalf" on Google's granular
    consent screen (then nothing can be sent) or no verified email came
    back. Pure."""
    if GMAIL_SEND_SCOPE not in str(tokens.get("scope") or "").split():
        return None
    claims = jwt_claims_unverified(tokens.get("id_token"))
    email = str(claims.get("email") or "").strip().lower()
    return email if "@" in email and claims.get("email_verified", True) not in (False, "false") else None


def is_rate_limited(response_text: str) -> bool:
    """A 403 that means "slow down", not "no permission". Pure."""
    lowered = response_text.lower()
    return "ratelimit" in lowered or "rate_limit" in lowered or "quota" in lowered


async def _access_token(client: httpx.AsyncClient, refresh_token: str) -> str:
    if not settings.GOOGLE_OAUTH_CLIENT_ID:
        raise MailSendError("google oauth not configured", TRY_AGAIN_PLAIN)
    try:
        response = await client.post(GOOGLE_TOKEN_URL, data={"client_id": settings.GOOGLE_OAUTH_CLIENT_ID, "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET, "refresh_token": refresh_token, "grant_type": "refresh_token"})
    except httpx.HTTPError as error:
        raise MailSendError(f"google token refresh failed: {type(error).__name__}", TRY_AGAIN_PLAIN) from error
    if response.status_code >= 400:
        # invalid_grant = revoked / expired / password-reset grant: only a reconnect fixes it
        revoked = response.status_code in (400, 401) and "invalid_grant" in response.text
        raise MailSendError(f"google token refresh -> {response.status_code}", RECONNECT_PLAIN if revoked else TRY_AGAIN_PLAIN, reconnect=revoked)
    return response.json()["access_token"]


async def send_gmail(refresh_token: str, raw_message: bytes, organization_id: str | None, brand_id: str | None, task_id: str | None) -> str:
    """Send one message; returns Gmail's message id. Raises MailSendError."""
    async with httpx.AsyncClient(timeout=SEND_TIMEOUT_SECONDS) as client:
        access_token = await _access_token(client, refresh_token)
        try:
            response = await client.post(GMAIL_SEND_URL, json={"raw": base64url(raw_message)}, headers={"Authorization": f"Bearer {access_token}"})
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            raise MailSendError(f"gmail send could not connect: {type(error).__name__}", TRY_AGAIN_PLAIN) from error
        except httpx.HTTPError as error:
            raise MailSendError(f"gmail send interrupted: {type(error).__name__}", MAYBE_SENT_PLAIN, maybe_sent=True) from error
    if response.status_code == 429 or (response.status_code == 403 and is_rate_limited(response.text)):
        raise MailSendError(f"gmail send -> {response.status_code} (rate limited)", TRY_AGAIN_PLAIN)
    if response.status_code in (401, 403):
        raise MailSendError(f"gmail send -> {response.status_code}", RECONNECT_PLAIN, reconnect=True)
    if response.status_code >= 400:
        raise MailSendError(f"gmail send -> {response.status_code}: {response.text[:200]}", RECONNECT_PLAIN if response.status_code < 500 else TRY_AGAIN_PLAIN)
    await CostEvent.create(adapter="mail_gmail", operation="send_pitch", organization_id=organization_id, brand_id=brand_id, task_id=task_id, usd=0.0, own_transaction=True)
    return str(response.json().get("id") or "")
