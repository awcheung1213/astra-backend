"""Resend send adapter — the evergreen route, for owners whose mailbox is
neither Google- nor Microsoft-hosted. The pitch goes out From
`{business name} <outreach@{EMAIL_FROM_DOMAIN}>` with Reply-To AND CC set to
the owner, so the stranger's reply and a copy of what was sent both land in
his own inbox.

The owner's address is NEVER put in From on this route: we cannot sign for
his domain, so DMARC would bounce or junk it.

API (verified 2026-10-13): POST https://api.resend.com/emails, bearer key,
JSON {from, to, subject, text, cc, reply_to}; answers {"id"}. Idempotency-Key
(24h) makes a retried request with the same key a no-op at Resend — we pass
the send's id. Price (verified 2026-10-13): Pro $20/mo for 50k emails,
overage $0.90 per 1,000 — the marginal per-email rate is what the cost_event
records (adapter "mail_resend")."""

from email.utils import formataddr

import httpx

import settings
from adapters.mail.common import MailSendError, header_safe
from objects.cost_event import CostEvent

RESEND_SEND_URL = "https://api.resend.com/emails"
RESEND_USD_PER_EMAIL = 0.0009
OUTREACH_LOCAL_PART = "outreach"
TIMEOUT_SECONDS = 30.0

TRY_AGAIN_PLAIN = "Our mail service had a problem sending it — try again in a few minutes."
REJECTED_PLAIN = "Our mail service didn't accept it — check their address and try again."
MAYBE_SENT_PLAIN = "We're not sure it went out — give it a few minutes before sending again."


def resend_configured() -> bool:
    return bool(settings.RESEND_API_KEY and settings.EMAIL_FROM_DOMAIN)


def outreach_address() -> str | None:
    return f"{OUTREACH_LOCAL_PART}@{settings.EMAIL_FROM_DOMAIN}" if settings.EMAIL_FROM_DOMAIN else None


def resend_payload(from_name: str, to_address: str, to_name: str | None, subject: str, body: str, owner_email: str | None) -> dict:
    """The /emails body: From our outreach@ address under his business name,
    Reply-To + CC him, plain text only. Pure."""
    payload: dict = {
        "from": formataddr((header_safe(from_name), f"{OUTREACH_LOCAL_PART}@{settings.EMAIL_FROM_DOMAIN}")),
        "to": [formataddr((header_safe(to_name), to_address)) if header_safe(to_name) else to_address],
        "subject": header_safe(subject),
        "text": body or "",
    }
    if owner_email:
        payload["reply_to"] = owner_email
        payload["cc"] = [owner_email]
    return payload


async def send_resend(payload: dict, idempotency_key: str, organization_id: str | None, brand_id: str | None, task_id: str | None) -> str:
    """Send one message; returns Resend's email id. Raises MailSendError."""
    if not resend_configured():
        raise MailSendError("resend not configured", TRY_AGAIN_PLAIN)
    headers = {"Authorization": f"Bearer {settings.RESEND_API_KEY}", "Idempotency-Key": idempotency_key}
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        try:
            response = await client.post(RESEND_SEND_URL, json=payload, headers=headers)
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            raise MailSendError(f"resend could not connect: {type(error).__name__}", TRY_AGAIN_PLAIN) from error
        except httpx.HTTPError as error:
            raise MailSendError(f"resend interrupted: {type(error).__name__}", MAYBE_SENT_PLAIN, maybe_sent=True) from error
    if response.status_code == 429 or response.status_code >= 500:
        raise MailSendError(f"resend -> {response.status_code}", TRY_AGAIN_PLAIN)
    if response.status_code >= 400:
        raise MailSendError(f"resend -> {response.status_code}: {response.text[:200]}", REJECTED_PLAIN)
    await CostEvent.create(adapter="mail_resend", operation="send_pitch", organization_id=organization_id, brand_id=brand_id, task_id=task_id, usd=RESEND_USD_PER_EMAIL, own_transaction=True)
    return str(response.json().get("id") or "")
