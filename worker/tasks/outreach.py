"""The Outreach scout (deliverables #13, #20, #21, #22): one scout, four
hunting grounds, selected by payload["scan"] (schedules.py fans them out on
their own cadences). The scout FINDS and DRAFTS — it never sends: every
opportunity lands as a pitch artifact behind the always-approve floor,
written as the owner, addressed only to an email address the scout saw
written on the target's own page (never guessed).

send_pitch is the other half: the owner approves a pitch ("Send to …"), and
after the 30-second unsend window this task emails it from his own mailbox
(gmail/outlook) or our outreach@ sender (utils/pitch_mail.py) — once, ever."""

from urllib.parse import urlparse

from adapters.agents.claude_agent import ClaudeAgent
from adapters.mail import gmail as gmail_mail
from adapters.mail import outlook as outlook_mail
from adapters.mail import resend as resend_mail
from adapters.mail.common import MailSendError, build_rfc2822, header_safe
from adapters.services.claude_service import ClaudeService
from objects.artifact import STATUS_APPROVED, STATUS_PENDING_REVIEW, Artifact
from objects.brand import Brand
from objects.connection import Connection
from objects.notification import KIND_ALERT, KIND_UPDATE, URGENCY_URGENT, Notification
from objects.site import STATUS_CONNECTED
from utils import clock
from utils.agent_json import parse_agent_json
from utils.agent_prompts import ROLE_OUTREACH_SCOUT
from utils.database import Database
from utils.logger import logger
from utils.pitch_mail import INTERRUPTED_PLAIN, ROUTE_EVERGREEN, ROUTE_GMAIL, ROUTE_OUTLOOK, clean_recipient, mark_failed, mark_sending, mark_sent, owner_email_for, send_decision
from utils.task_queue import RETRY_WITH_BACKOFF, task_lifecycle
from utils.token_crypto import decrypt_credentials, encrypt_credentials
from worker.app import app
from worker.tasks.site_audit import normalize_page_url


@app.task(name="outreach_scout", queue="outreach", retry=RETRY_WITH_BACKOFF)
async def outreach_scout(task_id: str, brand_id: str) -> None:
    """#13/#20/#21/#22 — web-search scout run for one hunting ground: finds
    opportunities, drafts the pitch + talking points for each, and emits one
    pitch artifact per opportunity behind the gate. Finding NOTHING is a valid
    outcome. Journalist requests (#20) additionally ring Pulse urgently —
    their deadlines are same-day."""
    async with task_lifecycle(task_id) as task:
        scan = (task.payload or {}).get("scan", "")
        scan_brief = SCAN_BRIEFS.get(scan)
        if scan_brief is None:
            raise ValueError(f"unknown outreach scan: {scan!r}")
        brand = await Brand.get(brand_id)

        agent_output = await ClaudeAgent().run_role(
            ROLE_OUTREACH_SCOUT,
            prompt=_scout_prompt(brand, scan_brief),
            tools=[WEB_SEARCH_TOOL],
            handlers={},  # web search is a server-side tool — nothing to handle
            operation="outreach_scout",
            organization_id=brand.organization_id,
            brand_id=brand.id,
            task_id=task_id,
        )
        opportunities = parse_agent_json(agent_output)
        if not isinstance(opportunities, list) or not all(isinstance(opportunity, dict) and opportunity.get("where") and opportunity.get("pitch") for opportunity in opportunities):
            raise ValueError(f"scout output is not a list of where+pitch opportunities: {agent_output[:200]}")

        live_pitches = await Artifact.for_brand(brand.id, type="pitch", statuses=[STATUS_PENDING_REVIEW, STATUS_APPROVED])
        opportunities = new_pitch_opportunities(opportunities, already_pitched={pitch_target_key(str(pitch.content.get("where", ""))) for pitch in live_pitches})
        opportunities = [normalize_pitch(opportunity, brand.name) for opportunity in opportunities[:OPPORTUNITY_LIMIT]]
        for opportunity in opportunities:
            await Artifact.create(task_id=task_id, brand_id=brand.id, type="pitch", content={"scan": scan, **opportunity})

        if scan == SCAN_JOURNALIST_REQUESTS and opportunities:
            await Notification.create(organization_id=brand.organization_id, kind=KIND_UPDATE, urgency=URGENCY_URGENT, title=f"{len(opportunities)} journalist request(s) worth answering today", link_ref="artifacts")

        logger.info(f"[outreach_scout] brand {brand.id} scan={scan}: {len(opportunities)} opportunities")


def pitch_target_key(where: str) -> str:
    """One spelling per pitch target: URLs via the audit's page normalizer,
    venues ("r/smallbusiness") by case-folded text."""
    where = where.strip()
    return normalize_page_url(where) if urlparse(where).scheme in ("http", "https") else where.lower()


def new_pitch_opportunities(opportunities: list[dict], already_pitched: set[str]) -> list[dict]:
    """Drop targets that already have a pending/approved pitch for the brand
    (DEC-015) and in-run repeats — one live pitch per target."""
    seen = set(already_pitched)
    fresh: list[dict] = []
    for opportunity in opportunities:
        key = pitch_target_key(str(opportunity["where"]))
        if key not in seen:
            seen.add(key)
            fresh.append(opportunity)
    return fresh


def normalize_pitch(opportunity: dict, business_name: str) -> dict:
    """One scout opportunity made safe to send: to_email kept ONLY when it is
    a sendable address (clean_recipient: no noreply, no example.com) AND the
    scout named the http(s) page it saw it on — otherwise the pitch stays,
    without an address (the owner can type one). Subject/name forced onto
    one line; a missing subject gets a plain default. Pure."""
    pitch = dict(opportunity)
    recipient = clean_recipient(pitch.get("to_email"))
    source = str(pitch.get("contact_source") or "").strip()
    if recipient is None or urlparse(source).scheme not in ("http", "https"):
        recipient, source = None, ""
    pitch["to_email"] = recipient
    pitch["contact_source"] = source or None
    pitch["to_name"] = header_safe(pitch.get("to_name")) or None
    pitch["subject"] = header_safe(pitch.get("subject")) or f"A note from {header_safe(business_name)}"
    pitch["pitch"] = str(pitch.get("pitch") or "").strip()
    return pitch


# payload["scan"] selects the scan; one scout, four hunting grounds:
# CAVEAT(#20): with no Connectively/Qwoted accounts, this scan can only
# web-search PUBLIC callouts — it is the shallowest of the four for now.
# TODO(#20): hook up the Connectively/Qwoted APIs as adapters and feed their
#       request feeds into this scan — that is where the real same-day
#       deadline volume lives.
SCAN_JOURNALIST_REQUESTS = "journalist_requests"  # #20 — daily, same-day deadlines
SCAN_CITATION_TARGETS = "citation_targets"  # #13 — weekly link/PR prospecting
SCAN_COMMUNITY_THREADS = "community_threads"  # #21 — weekly Reddit/Quora digest
SCAN_UNLINKED_MENTIONS = "unlinked_mentions"  # #22 — weekly reclamation

SCOUT_MAX_WEB_SEARCHES = 8  # per run — web searches are the scan's whole cost
OPPORTUNITY_LIMIT = 3  # NEW pitches per run — the send cap: three good emails beat ten mediocre ones
PITCH_MAX_WORDS = 120
SUBJECT_MAX_WORDS = 8

WEB_SEARCH_TOOL = {"type": ClaudeService.WEB_SEARCH_TOOL_TYPE, "name": "web_search", "max_uses": SCOUT_MAX_WEB_SEARCHES}

SCAN_BRIEFS = {
    SCAN_CITATION_TARGETS: ("Find pages and publications that recommend or list businesses in the brand's category — 'best X'\n" "roundups, resource pages, local guides — where this brand is ABSENT, plus outlets that cite its\n" "competitors. Each is a pitch target for inclusion or citation."),
    SCAN_JOURNALIST_REQUESTS: (
        "Find CURRENT journalist/source requests the brand could credibly answer (Connectively, Qwoted,\n" "#journorequest, reporter callouts). Deadlines are same-day: include the deadline whenever stated\n" "and skip anything already expired or outside the brand's real expertise."
    ),
    SCAN_COMMUNITY_THREADS: (
        "Find recent Reddit/Quora/forum threads where people ask for recommendations in the brand's\n" "category and a genuine, non-promotional contribution would actually help. Draft the contribution\n" "itself — helpful first, brand mentioned only where it is honestly the answer."
    ),
    SCAN_UNLINKED_MENTIONS: ("Find pages that mention the brand by name WITHOUT linking to its site. Each is a reclamation ask:\n" "a short, friendly note to the page owner requesting the link."),
}


def _scout_prompt(brand: Brand, scan_brief: str) -> str:
    return (
        f"{scan_brief}\n\n"
        f"Return your {OPPORTUNITY_LIMIT} best opportunities at most — fewer is fine, and an empty list is the\n"
        "correct answer when you find nothing genuinely worth the owner's name on an email.\n"
        "The pitch is an EMAIL the owner sends from his own mailbox, under his own name, to a stranger:\n"
        f"- written in first person as the owner of {brand.name}, plain and friendly, under {PITCH_MAX_WORDS} words;\n"
        f"  signed with the business name ({brand.name}) — never invent a personal name, title or phone number;\n"
        "- no links unless the target asked for one, no hype, no marketing phrases, no placeholders like [Name];\n"
        f"- a subject line under {SUBJECT_MAX_WORDS} words, plain, no clickbait.\n"
        "to_email: ONLY an address you saw WRITTEN on the target's own page or site (a contact page, an author\n"
        "bio, the callout itself) — put that page's URL in contact_source. NEVER guess or build an address from\n"
        "a pattern (firstname@domain); if you did not see one, omit to_email, to_name and contact_source.\n"
        "Respond with ONLY a JSON array, no prose and no code fences. Each entry:\n"
        '{"where": "url or venue", "why_fit": "why this brand is credible here", "subject": "short subject line",\n'
        ' "pitch": "the email body", "to_email": "address seen on their site, else omit", "to_name": "the person or\n'
        ' publication it goes to, else omit", "contact_source": "url where the address was seen, else omit",\n'
        ' "talking_points": ["..."], "deadline": "when stated, else omit"}\n\n'
        f"Brand: {brand.name} ({brand.website_url})\n"
        f"Category: {brand.category}\n"
        f"Description: {brand.description}\n"
        f"Audience: {brand.audience}\n"
        f"Differentiators: {brand.differentiators}\n"
        f"Footprint: {brand.footprint}"
    )


##############################
# SEND (the approved pitch goes out — once)
##############################

NOT_CONNECTED_PLAIN = {
    ROUTE_GMAIL: gmail_mail.RECONNECT_PLAIN,
    ROUTE_OUTLOOK: outlook_mail.RECONNECT_PLAIN,
}


@app.task(name="send_pitch", queue="outreach", retry=RETRY_WITH_BACKOFF)
async def send_pitch(task_id: str, brand_id: str) -> None:
    """CHAINED from a pitch approval (payload: artifact_id + send_id), deferred
    past the 30-second unsend window. Sends only if the pitch is STILL
    approved with THIS send scheduled (send_decision)."""
    async with task_lifecycle(task_id) as task:
        payload = task.payload or {}
        await deliver_pitch(str(payload.get("artifact_id") or ""), payload.get("send_id"), brand_id, task_id)


async def deliver_pitch(artifact_id: str, send_id: str | None, brand_id: str, task_id: str) -> None:
    """The once-only send. Under the artifact's row lock (unsend takes the
    same lock): decide, then COMMIT `sending` BEFORE the provider call — the
    completion marker. A retry or duplicate job that finds `sending` never
    sends again; it records "not sure it went out" instead. A failure leaves
    the pitch approved with send.status "failed" + a plain error (the
    frontend's "Try again" re-approves it — a fresh send id, the same 30s
    window, the same once-only guard), and the owner hears about it in Pulse."""
    artifact = await Artifact.get_for_update(artifact_id)
    content = dict(artifact.content or {})
    send: dict = content.get("send") or {}
    decision = send_decision(artifact.status, send, send_id)
    if decision == "skip":
        logger.info(f"[send_pitch] artifact {artifact_id}: nothing to send ({artifact.status}, send {send.get('status')})")
        return
    brand = await Brand.get(brand_id)
    if decision == "interrupted":
        await _record_failure(brand, artifact.id, content, send, INTERRUPTED_PLAIN)
        return

    sending = mark_sending(send, clock.now())
    await Artifact.set_content(artifact.id, {**content, "send": sending})
    await Database.session.commit()  # the marker must be durable before anything can go out

    try:
        provider_message_id = await _send_via_route(brand, content, sending, task_id)
    except MailSendError as error:
        logger.error(f"[send_pitch] artifact {artifact.id} via {sending.get('route')}: {error}")
        if error.reconnect and sending.get("route") in NOT_CONNECTED_PLAIN:
            await Connection.mark_disconnected(brand.id, sending["route"])
        await _record_failure(brand, artifact.id, content, sending, error.plain)
        return

    await Artifact.set_content(artifact.id, {**content, "send": mark_sent(sending, clock.now(), provider_message_id)})
    await Database.session.commit()
    logger.info(f"[send_pitch] artifact {artifact.id}: sent via {sending.get('route')}")


async def _record_failure(brand: Brand, artifact_id: str, content: dict, send: dict, plain_error: str) -> None:
    await Artifact.set_content(artifact_id, {**content, "send": mark_failed(send, plain_error, clock.now())})
    await Notification.create(organization_id=brand.organization_id, kind=KIND_ALERT, title=f"Your email to {send.get('to')} didn't go out", body=plain_error, link_ref=f"artifacts/{artifact_id}")
    await Database.session.commit()


async def _send_via_route(brand: Brand, content: dict, send: dict, task_id: str) -> str | None:
    """Hand the pitch to its route's adapter; returns the provider's message
    id (None for Outlook — Graph returns none). Everything that can fail
    BEFORE the provider call raises a definitive (not-sent) MailSendError."""
    route = send.get("route")
    recipient, to_name = send.get("to") or "", content.get("to_name")
    subject, body = content.get("subject") or "", content.get("pitch") or ""
    ids = {"organization_id": brand.organization_id, "brand_id": brand.id, "task_id": task_id}

    if route == ROUTE_EVERGREEN:
        if not resend_mail.resend_configured():
            raise MailSendError("resend not configured", resend_mail.TRY_AGAIN_PLAIN)
        payload = resend_mail.resend_payload(brand.name, recipient, to_name, subject, body, await owner_email_for(brand))
        return await resend_mail.send_resend(payload, idempotency_key=str(send.get("id")), **ids)

    if route not in NOT_CONNECTED_PLAIN:
        raise MailSendError(f"unknown route {route!r}", resend_mail.TRY_AGAIN_PLAIN)
    connection = await Connection.get_for_brand_provider(brand.id, route)
    if connection is None or connection.status != STATUS_CONNECTED or not connection.credentials_encrypted:
        raise MailSendError(f"no connected {route} mailbox", NOT_CONNECTED_PLAIN[route])
    try:
        credentials = decrypt_credentials(connection.credentials_encrypted)
        refresh_token = credentials["refresh_token"]
        mailbox = credentials.get("email") or send.get("from") or ""
    except Exception as error:
        raise MailSendError(f"{route} credentials unreadable: {type(error).__name__}", NOT_CONNECTED_PLAIN[route], reconnect=True) from error

    if route == ROUTE_GMAIL:
        raw = build_rfc2822(brand.name, mailbox, recipient, to_name, subject, body)
        return await gmail_mail.send_gmail(refresh_token, raw, **ids)

    rotated = await outlook_mail.send_outlook(refresh_token, outlook_mail.graph_send_payload(recipient, to_name, subject, body), **ids)
    if rotated:
        await Connection.update_credentials(brand.id, ROUTE_OUTLOOK, encrypt_credentials({**credentials, "refresh_token": rotated}))
    return None
