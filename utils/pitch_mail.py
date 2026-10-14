"""Outreach pitches that actually send — the shared core behind GET
email_sender, the pitch branch of the review endpoint, unsend, and the
send_pitch task.

Routes (chosen automatically, never by the owner):
- gmail     his own Gmail / Google Workspace mailbox (OAuth, gmail.send only)
- outlook   his own Outlook.com / Microsoft 365 mailbox (OAuth, Mail.Send)
- evergreen anything else: Resend, From `{business} <outreach@EMAIL_FROM_DOMAIN>`,
            Reply-To + CC him (never his address in From — DMARC)
- none      nothing configured: the frontend keeps copy-paste

The send lives on the pitch artifact as content["send"] (read whole with the
pitch — rule 4), a small state machine:

    scheduled --(30s, still approved)--> sending --> sent | failed
        |                                        ("Try again" on failed = a new scheduled send)
        \\--(unsend)--> cancelled (artifact back to pending_review)

    "I sent it" (no edits posted, nothing to send with) --> sent, route "manual"

`sending` is the completion marker (the change-set idea): it is COMMITTED
before the provider call, so a retried or duplicated job that finds it never
sends again. Each scheduling mints a send id carried by the job; a job whose
id no longer matches (unsend + re-approve) is stale and does nothing.

Everything above the RESOLVER section is pure (tests/test_pitch_mail.py)."""

import re
import uuid
from datetime import datetime, timedelta, timezone

import settings
from adapters import dns_adapter
from adapters.mail.common import header_safe
from adapters.mail.gmail import GMAIL_OPEN_URL
from adapters.mail.outlook import OUTLOOK_OPEN_URL, microsoft_configured
from adapters.mail.resend import outreach_address, resend_configured
from objects.artifact import STATUS_APPROVED
from schemas.base_schemas import utc_iso

ROUTE_GMAIL = "gmail"
ROUTE_OUTLOOK = "outlook"
ROUTE_EVERGREEN = "evergreen"
ROUTE_NONE = "none"
ROUTE_MANUAL = "manual"  # content.send.route only: the owner sent it himself and tapped "I sent it"
MAILBOX_ROUTES = (ROUTE_GMAIL, ROUTE_OUTLOOK)  # also the connections.provider values

HOST_GOOGLE = "google"
HOST_MICROSOFT = "microsoft"
GOOGLE_CONSUMER_DOMAINS = {"gmail.com", "googlemail.com"}
MICROSOFT_CONSUMER_DOMAINS = {"outlook.com", "hotmail.com", "live.com", "msn.com"}

SEND_SCHEDULED = "scheduled"
SEND_CANCELLED = "cancelled"
SEND_SENDING = "sending"
SEND_SENT = "sent"
SEND_FAILED = "failed"
SEND_DELAY_SECONDS = 30  # the undo window

OPEN_URLS = {ROUTE_GMAIL: GMAIL_OPEN_URL, ROUTE_OUTLOOK: OUTLOOK_OPEN_URL, ROUTE_EVERGREEN: None}

# owner-facing refusals (the frontend shows / maps these verbatim)
DETAIL_NEEDS_ADDRESS = "Add their email address first."
DETAIL_NOT_SET_UP = "Email sending isn't set up yet."
DETAIL_CONNECT_MAIL = "connect_mail"
DETAIL_ALREADY_SENT = "Already sent."
INTERRUPTED_PLAIN = "We're not sure it went out — check your Sent folder before sending again."


##############################
# WHO HOSTS HIS MAILBOX
##############################


def email_domain(email: str | None) -> str:
    return str(email or "").strip().lower().rsplit("@", 1)[-1] if "@" in str(email or "") else ""


def classify_mail_host(email: str | None, mx_hosts: list[str] | None) -> str | None:
    """ "google" | "microsoft" | None (unknown/other), from the address's
    domain and, for custom domains, its MX hosts. Pure."""
    domain = email_domain(email)
    if domain in GOOGLE_CONSUMER_DOMAINS:
        return HOST_GOOGLE
    if domain in MICROSOFT_CONSUMER_DOMAINS:
        return HOST_MICROSOFT
    for host in mx_hosts or []:
        host = host.lower().rstrip(".")
        if host in ("google.com", "googlemail.com") or host.endswith(".google.com") or host.endswith(".googlemail.com"):
            return HOST_GOOGLE
        if host.endswith(".protection.outlook.com"):
            return HOST_MICROSOFT
    return None


def select_route(connected_mailboxes: set[str], mail_host: str | None, google_configured: bool, microsoft_ready: bool, evergreen_ready: bool) -> tuple[str, bool]:
    """(route, connected). A connected mailbox wins; then the mailbox his
    address is hosted on (offered for connecting — only when that OAuth app
    is configured, or the connect step would dead-end); then our own sender;
    else none. Pure."""
    if ROUTE_GMAIL in connected_mailboxes:
        return ROUTE_GMAIL, True
    if ROUTE_OUTLOOK in connected_mailboxes:
        return ROUTE_OUTLOOK, True
    if mail_host == HOST_GOOGLE and google_configured:
        return ROUTE_GMAIL, False
    if mail_host == HOST_MICROSOFT and microsoft_ready:
        return ROUTE_OUTLOOK, False
    if evergreen_ready:
        return ROUTE_EVERGREEN, True
    return ROUTE_NONE, False


##############################
# RECIPIENTS (only addresses seen written on their site)
##############################

EMAIL_PATTERN = re.compile(r"^[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
UNREPLYABLE_LOCAL_PARTS = re.compile(r"^(no-?reply|do-?not-?reply|donotreply|noreply|mailer-daemon|postmaster|bounces?|abuse)([+._-]|$)")
PLACEHOLDER_DOMAINS = {"example.com", "example.org", "example.net", "domain.com", "yourdomain.com", "yoursite.com", "yourcompany.com", "email.com", "test.com", "sentry.io", "wixpress.com"}
PLACEHOLDER_LOCAL_PARTS = {"you", "your", "yourname", "name", "user", "username", "email", "john.doe", "jane.doe", "firstname.lastname"}
FILE_LIKE_TLDS = {"png", "jpg", "jpeg", "gif", "webp", "svg", "css", "js"}  # "logo@2x.png" scraped from markup


def clean_recipient(value) -> str | None:
    """A sendable address, normalized (trimmed, mailto: dropped, lower-cased),
    or None when it is malformed, unreplyable (noreply, postmaster), a
    placeholder (example.com, you@yourdomain.com) or a file name. Pure."""
    address = str(value or "").strip().strip("<>").strip()
    address = re.sub(r"^mailto:", "", address, flags=re.IGNORECASE).split("?", 1)[0].strip().lower()
    if not address or len(address) > 254 or not EMAIL_PATTERN.match(address):
        return None
    local_part, domain = address.rsplit("@", 1)
    if ".." in local_part or local_part.startswith(".") or local_part.endswith("."):
        return None
    if UNREPLYABLE_LOCAL_PARTS.match(local_part) or local_part in PLACEHOLDER_LOCAL_PARTS:
        return None
    if domain in PLACEHOLDER_DOMAINS or domain.endswith(".example") or domain.endswith(".invalid") or domain.endswith(".test") or domain.rsplit(".", 1)[-1] in FILE_LIKE_TLDS:
        return None
    return address


def apply_pitch_edits(content: dict, edits: dict | None) -> dict:
    """The owner's edits on a pending pitch, applied to a COPY of its content:
    only the keys he sent change (to_email, to_name, subject, body -> pitch).
    A hand-typed address no longer came from their site, so its
    contact_source is cleared. Pure."""
    updated = dict(content or {})
    if not edits:
        return updated
    if "to_email" in edits:
        typed = str(edits.get("to_email") or "").strip()
        if typed.lower() != str(updated.get("to_email") or "").strip().lower():
            updated["contact_source"] = None
        updated["to_email"] = typed or None
    if "to_name" in edits:
        updated["to_name"] = header_safe(edits.get("to_name")) or None
    if "subject" in edits:
        updated["subject"] = header_safe(edits.get("subject"))
    if "body" in edits:
        updated["pitch"] = str(edits.get("body") or "").strip()
    return updated


##############################
# THE SEND STATE MACHINE (content["send"])
##############################


def new_send_id() -> str:
    return str(uuid.uuid4())


def scheduled_send(route: str, from_address: str | None, to_address: str, now: datetime, send_id: str, subject: str = "", body: str = "") -> dict:
    """A send waiting out its unsend window. subject/body are copied in so
    the sent receipt shows exactly what went out."""
    return {"id": send_id, "status": SEND_SCHEDULED, "send_at": utc_iso(now + timedelta(seconds=SEND_DELAY_SECONDS)), "route": route, "from": from_address, "to": to_address, "subject": subject, "body": body}


def manual_send(now: datetime, send_id: str, to_address: str | None, subject: str = "", body: str = "") -> dict:
    """The owner sent it himself (copy-paste) and tapped "I sent it": recorded
    as sent, nothing for us to deliver. Pure."""
    return {"id": send_id, "status": SEND_SENT, "route": ROUTE_MANUAL, "sent_at": utc_iso(now), "from": None, "to": to_address, "subject": subject, "body": body, "provider_message_id": None, "open_url": None}


def pitch_review_mode(edits_given: bool, recipient: str | None, route: str) -> str:
    """How a pitch approval is handled. Pure.
    "manual"   — no edits posted and nothing to send with (route none or no
                 usable address): the owner's "I sent it" — record it sent.
    "send"     — schedule our send (the caller still 422s a missing
                 address and 409s a missing route / unconnected mailbox)."""
    if not edits_given and (route == ROUTE_NONE or recipient is None):
        return "manual"
    return "send"


def can_review_pitch(artifact_status: str, decision_is_approve: bool, send: dict | None) -> bool:
    """Pending pitches take either verdict; an APPROVED pitch whose send
    FAILED takes another approve (the "Try again" button). Pure."""
    if artifact_status == "pending_review":
        return True
    return decision_is_approve and artifact_status == STATUS_APPROVED and isinstance(send, dict) and send.get("status") == SEND_FAILED


def send_decision(artifact_status: str, send: dict | None, send_id: str | None) -> str:
    """What a send_pitch job does: "send" (still approved + scheduled, and the
    job belongs to THIS scheduling), "interrupted" (a previous attempt
    committed `sending` and never finished — it may have gone out: never send
    again), or "skip" (cancelled, already sent/failed, or a stale job). Pure."""
    if not isinstance(send, dict) or not send_id or send.get("id") != send_id:
        return "skip"
    if send.get("status") == SEND_SENDING:
        return "interrupted"
    if send.get("status") == SEND_SCHEDULED and artifact_status == STATUS_APPROVED:
        return "send"
    return "skip"


def mark_sending(send: dict, now: datetime) -> dict:
    return {**send, "status": SEND_SENDING, "sending_at": utc_iso(now)}


def mark_sent(send: dict, now: datetime, provider_message_id: str | None) -> dict:
    return {**send, "status": SEND_SENT, "sent_at": utc_iso(now), "provider_message_id": provider_message_id or None, "open_url": OPEN_URLS.get(send.get("route", ""))}


def mark_failed(send: dict, plain_error: str, now: datetime) -> dict:
    return {**send, "status": SEND_FAILED, "error": plain_error, "failed_at": utc_iso(now)}


def mark_cancelled(send: dict, now: datetime) -> dict:
    return {**send, "status": SEND_CANCELLED, "cancelled_at": utc_iso(now)}


def can_unsend(send: dict | None) -> bool:
    return isinstance(send, dict) and send.get("status") == SEND_SCHEDULED


def sent_at(content: dict | None) -> datetime | None:
    """When a pitch actually went out (naive UTC, like stored datetimes), or None. Pure."""
    send = (content or {}).get("send")
    if not isinstance(send, dict) or send.get("status") != SEND_SENT or not send.get("sent_at"):
        return None
    try:
        moment = datetime.fromisoformat(str(send["sent_at"]))
    except ValueError:
        return None
    return moment.astimezone(timezone.utc).replace(tzinfo=None) if moment.tzinfo is not None else moment


##############################
# RESOLVER (DB + MX lookup)
##############################


def mail_return_url(route: str, result: str) -> str:
    """Where a mailbox consent callback bounces the browser: the tasks screen,
    where the pitch he was sending waits. Pure."""
    return f"{settings.DASHBOARD_URL}/?screen=tasks&mail={route}&result={result}"


async def owner_email_for(brand) -> str | None:
    """The org owner's sign-in address (users.email of the org's first user),
    or None when it isn't a usable address (e.g. the guard's @unknown.invalid
    placeholder for a token with no email claim)."""
    from objects.user import User

    owner = await User.owner_for_organization(brand.organization_id)
    email = str(getattr(owner, "email", "") or "").strip().lower()
    return email if clean_recipient(email) else None


async def resolve_sender(brand) -> dict:
    """The GET email_sender payload for a brand: {route, connected,
    from_address, from_name, reply_to, owner_email}."""
    from objects.connection import Connection

    owner_email = await owner_email_for(brand)
    mailboxes = {connection.provider: connection for connection in await Connection.connected_for_brand(brand.id) if connection.provider in MAILBOX_ROUTES}
    mail_host = None
    if not mailboxes and owner_email:
        mail_host = classify_mail_host(owner_email, None)
        if mail_host is None:
            mail_host = classify_mail_host(owner_email, await dns_adapter.mx_hosts(email_domain(owner_email)))
    route, connected = select_route(set(mailboxes), mail_host, bool(settings.GOOGLE_OAUTH_CLIENT_ID), microsoft_configured(), resend_configured())

    from_address = None
    if route in MAILBOX_ROUTES and connected:
        from_address = (mailboxes[route].meta or {}).get("email")
    elif route == ROUTE_EVERGREEN:
        from_address = outreach_address()
    return {
        "route": route,
        "connected": connected,
        "from_address": from_address,
        "from_name": brand.name,
        "reply_to": owner_email if route == ROUTE_EVERGREEN else None,
        "owner_email": owner_email,
    }
