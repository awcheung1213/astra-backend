"""Receipt additions for the redesigned screens: the Your-tasks page's
"recent" decisions list (GET /brands/{id}/approvals) and the extra facts on
one site change (GET /change_sets/{id}: when it was proposed, and how big a
newly published page is).

Everything above the GLUE section is PURE (rows in, dicts out), unit-tested
without a DB (tests/test_receipt_facts.py). Titles reuse utils/receipts.py's
owner-language helpers and follow the Marco test (customer-persona.md)."""

import re
from types import SimpleNamespace
from urllib.parse import urlparse

from objects.artifact import STATUS_APPROVED, STATUS_AUTO_APPROVED, STATUS_REJECTED, Artifact
from objects.change_set import ChangeSet
from schemas.base_schemas import utc_iso
from utils import receipts
from utils.pitch_mail import ROUTE_MANUAL, SEND_CANCELLED, SEND_SENT
from utils.pitch_mail import sent_at as pitch_sent_at
from worker.tasks.site_audit import visible_text

RECENT_LIMIT = 10
DECIDED_STATUSES = [STATUS_APPROVED, STATUS_AUTO_APPROVED, STATUS_REJECTED]  # superseded = retired unreviewed, nobody decided
# born decided, never a decision anyone made (approval_flow: informational types never sit at pending review)
INFORMATIONAL_TYPES = {"brief", "diagnosis", "flag_request"}
SITE_FIX_TYPES = {"change_proposal", "draft"}  # approval becomes a change set (approval_flow.SITE_APPLIED_ARTIFACT_TYPES)

DECISION_APPROVED = "approved"
DECISION_REJECTED = "rejected"
DECISION_AUTO_APPROVED = "auto_approved"
DECISION_SENT = "sent"
DECISION_MARKED_SENT = "marked_sent"  # the owner sent it himself and tapped "I sent it"
DECISION_CANCELLED = "cancelled"
DECISION_POSTED_YOURSELF = "posted_yourself"  # Google posts with no GBP connection: he copied and posted them
DECISION_SENT_YOURSELF = "sent_yourself"  # a review-ask text he copied and sent


##############################
# RECENT DECISIONS
##############################


def _host(url) -> str:
    return urlparse(url).netloc.removeprefix("www.") if receipts.is_url(url) else ""


def decision(artifact) -> str:
    """What happened to a decided artifact, in one word the screen maps."""
    if artifact.status == STATUS_REJECTED:
        return DECISION_REJECTED
    if artifact.status == STATUS_AUTO_APPROVED:
        return DECISION_AUTO_APPROVED
    if artifact.type == "gbp_touches" and (artifact.content or {}).get("posted_by") == "owner":
        return DECISION_POSTED_YOURSELF
    if artifact.type == "review_ask":
        return DECISION_SENT_YOURSELF
    send = (artifact.content or {}).get("send") if artifact.type == "pitch" else None
    if isinstance(send, dict):
        if send.get("status") == SEND_SENT:
            return DECISION_MARKED_SENT if send.get("route") == ROUTE_MANUAL else DECISION_SENT
        if send.get("status") == SEND_CANCELLED:
            return DECISION_CANCELLED
    return DECISION_APPROVED


def decided_at(artifact):
    """The verdict moment (a sent pitch: when it went out); rows born decided fall back to birth."""
    if decision(artifact) in (DECISION_SENT, DECISION_MARKED_SENT):
        moment = pitch_sent_at(artifact.content)
        if moment is not None:
            return moment
    return artifact.reviewed_at or artifact.created_at


def decision_title(artifact, change_set=None) -> str:
    """The thing that was decided, named the way its approval card named it."""
    content = artifact.content or {}
    if artifact.type == "change_proposal":
        # display.title first (headline_present), else derived from the ops — the
        # resulting change set's when there is one (normalized op/target keys)
        return receipts.headline_present(change_set if change_set is not None else SimpleNamespace(operations=content.get("operations") or []), artifact)
    if artifact.type == "pitch":
        recipient = str(((content.get("send") or {}).get("to")) or content.get("to_email") or "")
        place = _host(content.get("where")) or (recipient.rsplit("@", 1)[-1] if "@" in recipient else "")
        return f"Email to {place}" if place else "Email to a website"
    if artifact.type == "draft":
        if content.get("kind") == "refresh":
            return f"A fresher version of {receipts.page_phrase(content.get('page'))}"
        return f"New page: {content['title']}" if content.get("title") else "A new page for your site"
    if artifact.type == "calendar":
        count = len(content.get("entries") or [])
        return f"{count} new articles for next month" if count else "Next month's articles"
    if artifact.type == "plan":
        return "Your plan for the next 3 months"
    if artifact.type == "reply":
        rating = (artifact.evidence or {}).get("rating")
        return f"Reply to a {rating}-star review" if isinstance(rating, int) else "Reply to a review"
    if artifact.type == "gbp_touches":
        count = len(content.get("posts") or [])
        return f"{count} posts for your Google Business Profile" if count else "Posts for your Google Business Profile"
    if artifact.type == "review_ask_templates":
        return "Messages asking customers for reviews"
    if artifact.type == "review_ask":
        return "A text asking customers for Google reviews"
    return "Work we did for you"


def resulting_change_set(change_sets: list):
    """Of ONE artifact's change sets: the live forward set, else the newest
    forward set (pending/failed/undone), else None. Undos never speak for it."""
    live_id = receipts.live_fix_change_set_id(change_sets)
    if live_id:
        return next(change_set for change_set in change_sets if change_set.id == live_id)
    reverted_from = receipts.revert_links(change_sets)
    forward = [change_set for change_set in change_sets if receipts.undo_depth(change_set.id, reverted_from) == 0]
    return max(forward, key=lambda change_set: change_set.created_at) if forward else None


def recent_entry(artifact, change_sets: list) -> dict:
    """One row of approvals.recent. `change_sets` = this artifact's sets (any)."""
    change_set = resulting_change_set(change_sets) if artifact.type in SITE_FIX_TYPES else None
    return {
        "id": artifact.id,
        "type": artifact.type,
        "title": decision_title(artifact, change_set),
        "decision": decision(artifact),
        "decided_at": utc_iso(decided_at(artifact)),
        "change_set_id": change_set.id if change_set is not None else None,
    }


def recent_entries(artifacts: list, change_sets: list, limit: int = RECENT_LIMIT) -> list[dict]:
    """Newest decision first, capped."""
    by_artifact: dict[str, list] = {}
    for change_set in change_sets:
        by_artifact.setdefault(change_set.artifact_id, []).append(change_set)
    entries = [recent_entry(artifact, by_artifact.get(artifact.id, [])) for artifact in artifacts if artifact.status in DECIDED_STATUSES and artifact.type not in INFORMATIONAL_TYPES]
    entries.sort(key=lambda entry: entry["decided_at"] or "", reverse=True)
    return entries[:limit]


##############################
# ONE SITE CHANGE: proposed_at + page_facts
##############################

MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\](\([^)]*\)|\[[^\]]*\])")
HTML_IMAGE = re.compile(r"<img\b[^>]*>", re.I)
MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
MARKDOWN_SYNTAX = re.compile(r"[#*_`>~|]+")
ORDERED_LIST_MARKER = re.compile(r"^\s*\d+[.)]\s+", re.M)
WORD = re.compile(r"[^\W_]", re.UNICODE)  # a token is a word when it holds a letter or digit


def photo_count(body: str | None) -> int:
    """Images in a page body: markdown images (inline or reference) + <img> tags."""
    if not body:
        return 0
    return len(MARKDOWN_IMAGE.findall(body)) + len(HTML_IMAGE.findall(body))


def word_count(body: str | None) -> int:
    """Words a visitor reads: images, tags, scripts, link targets and markdown
    syntax stripped; a token counts when it holds a letter or digit."""
    if not body:
        return 0
    text = HTML_IMAGE.sub(" ", MARKDOWN_IMAGE.sub(" ", body))
    text = ORDERED_LIST_MARKER.sub(" ", MARKDOWN_LINK.sub(r" \1 ", text))
    text = MARKDOWN_SYNTAX.sub(" ", visible_text(text))
    return sum(1 for token in text.split() if WORD.search(token))


def _page_body(value) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in ("body_markdown", "body_html", "content"):
        if isinstance(value.get(key), str):
            return value[key]
    return None


def page_facts(change_set) -> dict | None:
    """{word_count, photo_count} of the page a publish change set put live, or
    None when the set publishes no page (other change types omit page_facts)."""
    for operation in change_set.operations or []:
        if operation.get("op") == "publish_page":
            body = _page_body(operation.get("value"))
            return {"word_count": word_count(body), "photo_count": photo_count(body)}
    return None


def receipt_additions(change_set, artifact) -> dict:
    """The keys GET /change_sets/{id} adds to the receipt: proposed_at (the
    source artifact's birth) and, for a published page, page_facts."""
    additions: dict = {"proposed_at": utc_iso(artifact.created_at) if artifact is not None else None}
    facts = page_facts(change_set)
    if facts is not None:
        additions["page_facts"] = facts
    return additions


##############################
# GLUE (DB)
##############################


async def recent_decisions(brand_id: str, limit: int = RECENT_LIMIT) -> list[dict]:
    """approvals.recent for a brand: the last `limit` decided artifacts."""
    artifacts = await Artifact.decided_for_brand(brand_id, statuses=DECIDED_STATUSES, exclude_types=INFORMATIONAL_TYPES, limit=limit)
    change_sets = await ChangeSet.for_artifacts(artifact.id for artifact in artifacts if artifact.type in SITE_FIX_TYPES)
    return recent_entries(artifacts, change_sets, limit=limit)
