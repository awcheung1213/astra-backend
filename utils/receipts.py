"""Owner-language receipts — the pure derivations behind the Work page's
issues list, the done-for-you feed, the change ledger + change detail, and the
fix lifecycle (an issue closes the moment its fix goes live and reopens if the
fix is undone — marco-review.md R5).

Everything here is PURE (rows in, dicts/strings out — callers fetch), so it is
unit-tested without a DB (tests/test_receipts.py). Copy rules come from
customer-persona.md (the Marco test): plain words a plumber understands. Never
"schema", "ld+json", "meta description", "change set", "prompt", "sweep",
"queue" or "workstream" in anything this module returns to the screen."""

import json
import re
from datetime import datetime
from urllib.parse import quote, unquote, urlparse

from objects.artifact import STATUS_APPROVED, STATUS_AUTO_APPROVED, STATUS_PENDING_REVIEW, STATUS_REJECTED, STATUS_SUPERSEDED
from objects.change_set import STATUS_APPLIED, STATUS_FAILED, STATUS_REVERTED
from objects.finding import SEVERITY_SERIOUS, STATUS_FIXED, STATUS_OPEN
from schemas.base_schemas import utc_iso
from worker.tasks.site_audit import FIXABLE_KINDS, KIND_BLOCKED_AI_CRAWLER, KIND_BROKEN_LINK, KIND_MISSING_H1, KIND_MISSING_META_DESCRIPTION, KIND_MISSING_SCHEMA, KIND_MISSING_SITEMAP, KIND_MISSING_TITLE, KIND_SLOW_PAGE, visible_text

TEXT_MAX_CHARS = 160  # before/after snippets on the change detail
CARD_MAX_CHARS = 220  # the plain words a business card carries

# finding.detail markers the fix lifecycle writes (rule 5: machine rows, ours to revise)
RESOLVED_BY_KEY = "resolved_by_change_set_id"  # closed because this fix went live
FIX_DID_NOT_TAKE_KEY = "fix_did_not_take"  # the audit still saw the problem with the fix live


##############################
# PAGES
##############################

SITE_WIDE_KINDS = {KIND_BLOCKED_AI_CRAWLER, KIND_MISSING_SITEMAP}


def is_url(value) -> bool:
    return isinstance(value, str) and urlparse(value).scheme in ("http", "https")


def page_label(url: str | None, kind: str | None = None) -> str:
    """'Homepage', 'About page', or 'Whole site' for site-wide problems: the
    page's last path segment in words, never a raw path."""
    if kind in SITE_WIDE_KINDS:
        return "Whole site"
    if not is_url(url):
        return "Your site"
    segments = [segment for segment in (urlparse(url or "").path or "/").split("/") if segment]
    if not segments:
        return "Homepage"
    words = re.sub(r"\.(html?|php|aspx?)$", "", unquote(segments[-1])).replace("-", " ").replace("_", " ").strip()
    return f"{_capitalize(words)} page" if words else "Homepage"


def page_phrase(url: str | None, kind: str | None = None) -> str:
    """The page inside a sentence: 'your homepage', 'your /about/ page', 'your site'."""
    label = page_label(url, kind)
    if label == "Homepage":
        return "your homepage"
    if label in ("Whole site", "Your site"):
        return "your site"
    return f"your {_lower_first(label)}"


def _capitalize(text: str) -> str:
    return text[:1].upper() + text[1:]


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


def truncate(text: str | None, limit: int = TEXT_MAX_CHARS) -> str | None:
    """Whitespace collapsed, cut at a word boundary with an ellipsis. None/empty -> None."""
    if text is None:
        return None
    text = re.sub(r"\s+", " ", str(text)).strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"


##############################
# ISSUES (findings in plain words)
##############################

AI_ENGINE_NAMES = {"GPTBot": "ChatGPT", "ClaudeBot": "Claude", "Google-Extended": "Google's AI", "PerplexityBot": "Perplexity"}

ISSUE_WHY = {
    KIND_MISSING_TITLE: "The title is the headline people click in Google, and without one Google makes one up.",
    KIND_MISSING_META_DESCRIPTION: "It's the short blurb under your link in Google, and without one Google picks random text from the page.",
    KIND_MISSING_H1: "A clear main heading tells visitors and Google what the page is about.",
    KIND_MISSING_SCHEMA: "It tells Google and AI assistants your business name and what you do, without changing what visitors see.",
    KIND_SLOW_PAGE: "Slow pages lose callers before they finish loading, and Google shows them lower.",
    KIND_BROKEN_LINK: "People who land on a broken page leave, and Google counts it against your site.",
    KIND_BLOCKED_AI_CRAWLER: "If it can't read your site, it can't recommend you when people ask it for help.",
    KIND_MISSING_SITEMAP: "A page list helps Google find every page on your site, including new ones.",
}
DEFAULT_WHY = "Problems like this can make it harder for customers to find you."

# kinds no fixer handles: why we can't, in one plain sentence
CANT_FIX_REASONS = {
    KIND_SLOW_PAGE: "Speed depends on your web host and how the site was built, so editing the page won't fix it.",
    KIND_BROKEN_LINK: "We can't repair broken pages automatically yet: the page needs to be brought back or the link to it removed.",
    KIND_BLOCKED_AI_CRAWLER: "This is a setting in a file on your web server, which we can't change from your pages.",
    KIND_MISSING_SITEMAP: "Your site needs a page-list feature turned on (most free SEO plugins add one), which we can't do from your pages.",
}

# the Technical agent's skip reasons are written for engineers; these turn the
# common ones into one owner sentence (first match wins)
SKIP_REASON_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("no site is connected", "not connected", "no connected site"), "Connect your website so we can fix this for you."),
    (("plugin", "yoast", "rank math", "rank_math", "aioseo", "seopress"), "Your site needs a free SEO plugin before we can set this."),
    (("theme",), "This is controlled by your site's design theme, which we can't change from your pages."),
    (("robots", "server", "hosting", "web host", "cdn", "dns", "htaccess"), "This is a setting on your web server, which we can't change from your pages."),
    (("404", "not found", "unreachable", "does not exist", "doesn't exist"), "We couldn't open this page to fix it."),
)
SKIP_REASON_FALLBACK = "This needs a change outside your pages, so we can't make it automatically."
JARGON = {
    "meta", "schema", "json", "ld", "json-ld", "excerpt", "markup", "tag", "tags", "block", "gutenberg", "html", "script", "field", "cms", "seo",
    "crawler", "index", "indexing", "canonical", "robots", "sitemap", "h1", "api", "rest", "endpoint", "structured", "slug", "metafield", "noindex",
}  # fmt: skip


def has_jargon(text: str) -> bool:
    return any(word in JARGON for word in re.findall(r"[a-z0-9][a-z0-9+-]*", text.lower()))


def plain_skip_reason(reason: str | None) -> str | None:
    """The agent's skip reason as one sentence Marco can read, or None when there is none."""
    if not reason or not reason.strip():
        return None
    lowered = reason.lower()
    for needles, plain in SKIP_REASON_RULES:
        if any(needle in lowered for needle in needles):
            return plain
    first_sentence = re.split(r"(?<=[.!?])\s", reason.strip(), maxsplit=1)[0]
    if has_jargon(first_sentence) or len(first_sentence) > TEXT_MAX_CHARS:
        return SKIP_REASON_FALLBACK
    return first_sentence


def issue_title(kind: str, detail: dict | None, summary: str) -> str:
    """One plain line: 'Google shows no description for your homepage'."""
    detail = detail or {}
    url = detail.get("url")
    page = page_phrase(url, kind)
    if kind == KIND_MISSING_TITLE:
        return f"Google has no title to show for {page}"
    if kind == KIND_MISSING_META_DESCRIPTION:
        return f"Google shows no description for {page}"
    if kind == KIND_MISSING_H1:
        return f"{_capitalize(page)} has no main heading"
    if kind == KIND_MISSING_SCHEMA:
        return f"{_capitalize(page)} has no business card for search engines"
    if kind == KIND_SLOW_PAGE:
        seconds = detail.get("elapsed_seconds")
        return f"{_capitalize(page)} is slow to load ({float(seconds):.1f} seconds)" if isinstance(seconds, (int, float)) else f"{_capitalize(page)} is slow to load"
    if kind == KIND_BROKEN_LINK:
        return "Your homepage won't load" if page_label(url) == "Homepage" else f"A page on your site is broken: {page_label(url)}"
    if kind == KIND_BLOCKED_AI_CRAWLER:
        return f"Your site blocks {AI_ENGINE_NAMES.get(detail.get('crawler', ''), 'an AI assistant')} from reading it"
    if kind == KIND_MISSING_SITEMAP:
        return "Your site has no page list for search engines"
    return summary  # human/detector findings already carry a client-readable one-liner


def issue_why(kind: str) -> str:
    return ISSUE_WHY.get(kind, DEFAULT_WHY)


##############################
# FIX LIFECYCLE (proposal -> change sets -> live?)
##############################


def proposal_finding_ids(content: dict | None) -> list[str]:
    """The findings a fix proposal is for (content.finding_id, or a finding_ids list)."""
    content = content or {}
    ids = [content["finding_id"]] if isinstance(content.get("finding_id"), str) and content["finding_id"] else []
    ids += [finding_id for finding_id in content.get("finding_ids") or [] if isinstance(finding_id, str) and finding_id not in ids]
    return ids


def revert_links(change_sets: list) -> dict[str, str]:
    """revert set id -> the set it undoes (a revert reuses the original's artifact)."""
    return {change_set.reverted_by_change_set_id: change_set.id for change_set in change_sets if change_set.reverted_by_change_set_id}


def undo_depth(change_set_id: str, reverted_from: dict[str, str]) -> int:
    """0 = a forward change; 1 = an undo of it; 2 = an undo of the undo (forward again)."""
    depth, seen = 0, set()
    while change_set_id in reverted_from and change_set_id not in seen:
        seen.add(change_set_id)
        change_set_id = reverted_from[change_set_id]
        depth += 1
    return depth


def live_fix_change_set_id(change_sets: list) -> str | None:
    """Of ONE artifact's change sets: the forward set whose writes are live on
    the site now, or None. An Undo click flips the original to `reverted` at
    once, so a forward set still `applied` is live by definition."""
    reverted_from = revert_links(change_sets)
    live = [change_set for change_set in change_sets if change_set.status == STATUS_APPLIED and undo_depth(change_set.id, reverted_from) % 2 == 0]
    if not live:
        return None
    return max(live, key=lambda change_set: change_set.applied_at or change_set.created_at or datetime.min).id


def finding_fix_transition(status: str, detail: dict | None, live_change_set_id: str | None, chain_ids: set[str]) -> str | None:
    """What a finding should do after one of its fix's change sets applied:
    'resolve' (fix live, finding open), 'reopen' (fix no longer live and the
    finding was closed BY this fix), or None. Idempotent by construction — a
    second call after the first took effect returns None."""
    detail = detail or {}
    if live_change_set_id and status == STATUS_OPEN:
        return "resolve"
    if not live_change_set_id and status == STATUS_FIXED and detail.get(RESOLVED_BY_KEY) in chain_ids:
        return "reopen"
    return None


def resolved_detail(detail: dict | None, change_set_id: str) -> dict:
    """detail after a fix closed the finding (a new dict, so the JSONB change is detected)."""
    updated = {key: value for key, value in (detail or {}).items() if key != FIX_DID_NOT_TAKE_KEY}
    updated[RESOLVED_BY_KEY] = change_set_id
    return updated


def reopened_detail(detail: dict | None) -> dict:
    """detail after the fix was undone: the fix markers are gone, the facts stay."""
    return {key: value for key, value in (detail or {}).items() if key not in (RESOLVED_BY_KEY, FIX_DID_NOT_TAKE_KEY)}


##############################
# ISSUE STATUS
##############################

STATUS_LABELS = {
    "fix_live": "Fixed, live on your site",
    "fixed": "Fixed",
    "fix_waiting": "Fix ready, waiting for your OK",
    "cant_fix": "We can't fix this automatically",
}
DOTS = {"open": "yellow", "fix_waiting": "yellow", "fix_live": "green", "fixed": "green", "cant_fix": "grey"}
OPEN_ORDER = {"fix_waiting": 0, "open": 1, "cant_fix": 2}


def _pick_proposal(proposals: list, change_sets_by_artifact: dict[str, list]) -> tuple:
    """The proposal that speaks for the finding: one whose fix is live, else
    the newest. Returns (proposal | None, its change sets, live id | None)."""
    for proposal in proposals:
        live_id = live_fix_change_set_id(change_sets_by_artifact.get(proposal.id, []))
        if live_id:
            return proposal, change_sets_by_artifact.get(proposal.id, []), live_id
    if not proposals:
        return None, [], None
    newest = max(proposals, key=lambda proposal: proposal.created_at or datetime.min)
    return newest, change_sets_by_artifact.get(newest.id, []), None


def issue_status(finding, proposal, proposal_change_sets: list, live_id: str | None) -> tuple[str, str, str | None]:
    """(status, status_label, status_reason) for one finding. Pure."""
    detail = finding.detail or {}
    if finding.status == STATUS_FIXED:
        if live_id:
            return "fix_live", STATUS_LABELS["fix_live"], None
        return "fixed", STATUS_LABELS["fixed"], "This problem no longer shows up on your site."
    if detail.get(FIX_DID_NOT_TAKE_KEY) and live_id:
        return "open", "Still showing after our fix", "We made a change here, but the problem still shows up, so it needs a closer look."
    if live_id:
        return "fix_live", STATUS_LABELS["fix_live"], None  # rows from before close-on-apply existed
    skipped = detail.get("skipped")
    if skipped:
        reason = plain_skip_reason(skipped.get("reason") if isinstance(skipped, dict) else None)
        return "cant_fix", STATUS_LABELS["cant_fix"], reason or CANT_FIX_REASONS.get(finding.kind, SKIP_REASON_FALLBACK)
    if proposal is not None:
        if proposal.status == STATUS_PENDING_REVIEW:
            return "fix_waiting", STATUS_LABELS["fix_waiting"], None
        if proposal.status == STATUS_REJECTED:
            return "open", "You turned down our fix", None
        if proposal.status in (STATUS_APPROVED, STATUS_AUTO_APPROVED):
            if any(change_set.status == STATUS_REVERTED for change_set in proposal_change_sets):
                return "open", "You undid our fix", "Your page is back the way it was, so this problem is back too."
            if not proposal_change_sets:
                if not (proposal.content or {}).get("operations"):
                    return "open", "Fix approved, not on your site yet", "This one has to be made by hand on your site."
                return "open", "Fix approved, not on your site yet", "Connect your website so we can put it live."
            if any(change_set.status == STATUS_FAILED for change_set in proposal_change_sets):
                return "open", "Fix approved, not on your site yet", "Something went wrong putting it on your site."
            return "open", "Fix approved, going live shortly", None
    if finding.kind not in FIXABLE_KINDS and finding.kind in CANT_FIX_REASONS:
        return "cant_fix", STATUS_LABELS["cant_fix"], CANT_FIX_REASONS[finding.kind]
    return "open", "Not fixed yet", None


def issue_entry(finding, proposals: list, change_sets_by_artifact: dict[str, list]) -> dict:
    """One row of worklog.issues. `proposals` = this finding's fix proposals
    (any status); `change_sets_by_artifact` = artifact id -> its change sets."""
    detail = finding.detail or {}
    url = detail.get("url")
    proposal, proposal_change_sets, live_id = _pick_proposal(proposals, change_sets_by_artifact)
    status, label, reason = issue_status(finding, proposal, proposal_change_sets, live_id)
    return {
        "id": finding.id,
        "kind": finding.kind,
        "severity": finding.severity,
        "page_url": url if is_url(url) else None,
        "page_label": page_label(url, finding.kind),
        "title": issue_title(finding.kind, detail, finding.summary),
        "why_it_matters": issue_why(finding.kind),
        "status": status,
        "status_label": label,
        "status_reason": reason,
        "dot": DOTS[status],
        "artifact_id": proposal.id if proposal is not None else None,
        "change_set_id": live_id,
        "found_at": utc_iso(finding.created_at),
        "resolved_at": utc_iso(finding.resolved_at),
    }


def order_issues(entries: list[dict], limit: int = 50) -> list[dict]:
    """Open first (fix waiting on you, then open, then can't-fix; serious
    first, newest first), then the resolved, most recently resolved first."""
    open_entries = [entry for entry in entries if entry["status"] in OPEN_ORDER]
    resolved_entries = [entry for entry in entries if entry["status"] not in OPEN_ORDER]
    open_entries.sort(key=lambda entry: entry["found_at"] or "", reverse=True)
    open_entries.sort(key=lambda entry: (OPEN_ORDER[entry["status"]], entry.get("severity") != SEVERITY_SERIOUS))
    resolved_entries.sort(key=lambda entry: entry["resolved_at"] or "", reverse=True)
    return (open_entries + resolved_entries)[:limit]


##############################
# CHANGES (headlines, ledger entries, plain before/after)
##############################

CONTENT_FIELDS = {"content", "body_html"}
DESCRIPTION_FIELDS = {"meta_description", "excerpt"}
PAST_TENSE = {
    "Add": "Added", "Set": "Set", "Update": "Updated", "Make": "Made", "Publish": "Published", "Rewrite": "Rewrote", "Take": "Took", "Fix": "Fixed",
    "Change": "Changed", "Remove": "Removed", "Write": "Wrote", "Replace": "Replaced", "Create": "Created", "Improve": "Improved", "Give": "Gave",
}  # fmt: skip
LD_JSON_BLOCK = re.compile(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.I | re.S)


def past_tense(headline: str) -> str:
    """'Add a business card …' -> 'Added a business card …'."""
    first, _, rest = headline.partition(" ")
    if first in PAST_TENSE:
        return f"{PAST_TENSE[first]} {rest}".strip()
    return headline


def ld_json_blocks(html) -> list[str]:
    return LD_JSON_BLOCK.findall(html) if isinstance(html, str) else []


def card_change(previous, value) -> str | None:
    """'added' / 'removed' when a content write only adds or removes a business
    card block (the visible text is untouched), else None."""
    new_blocks = ld_json_blocks(value)
    if previous is None:
        return "added" if new_blocks and isinstance(value, str) else None  # rollback record not captured yet
    old_blocks = ld_json_blocks(previous)
    same_visible_text = visible_text(previous or "") == visible_text(value or "") if isinstance(previous, str) and isinstance(value, str) else False
    if len(new_blocks) > len(old_blocks) and same_visible_text:
        return "added"
    if len(old_blocks) > len(new_blocks) and same_visible_text:
        return "removed"
    return None


def business_card_words(blocks: list[str], limit: int = CARD_MAX_CHARS) -> str:
    """What the card tells search engines, in plain words (name, what you do, phone, address)."""
    for block in blocks:
        try:
            data = json.loads(block)
        except ValueError:
            continue
        nodes = data if isinstance(data, list) else [data]
        flat = [item for node in nodes if isinstance(node, dict) for item in [node] + [graph for graph in node.get("@graph", []) if isinstance(graph, dict)]]
        for node in flat:
            if not isinstance(node.get("name"), str):
                continue
            parts = [node["name"].strip()]
            if isinstance(node.get("description"), str) and node["description"].strip():
                parts.append(node["description"].strip())
            if isinstance(node.get("telephone"), str):
                parts.append(f"Phone: {node['telephone']}")
            address = node.get("address")
            if isinstance(address, dict):
                line = ", ".join(str(address[key]) for key in ("streetAddress", "addressLocality", "addressRegion") if address.get(key))
                if line:
                    parts.append(f"Address: {line}")
            elif isinstance(address, str) and address.strip():
                parts.append(f"Address: {address.strip()}")
            return truncate(". ".join(part.rstrip(".") for part in parts) + ".", limit) or ""
    return "Your business name and what you do."


def _changed_part(previous: str | None, value: str | None) -> tuple[str | None, str | None]:
    """Plain before/after snippets of a text change, starting where they differ."""
    before_words = (previous or "").split()
    after_words = (value or "").split()
    common = 0
    while common < min(len(before_words), len(after_words)) and before_words[common] == after_words[common]:
        common += 1
    lead = "…" if common > 3 else ""
    start = max(common - 3, 0) if lead else 0
    before = truncate(lead + " ".join(before_words[start:])) if before_words else None
    after = truncate(lead + " ".join(after_words[start:])) if after_words else None
    return before, after


def _plain(value) -> str | None:
    if value is None:
        return None
    return truncate(visible_text(str(value)))


def card_check_url(url: str | None) -> str | None:
    """Proof Marco can open for a change visitors can't see: the schema.org
    validator reads the live page and lists the card (name, site, description).
    Not Google's Rich Results Test — it only reports rich-result types and
    shows "No items detected" for a plain Organization card."""
    return f"https://validator.schema.org/#url={quote(url, safe='')}" if is_url(url) else None


def describe_operation(operation: dict, page_url: str | None = None) -> dict:
    """One entry of the change detail: page, what changed in a plain sentence,
    short plain before/after (never raw HTML), and whether visitors see it."""
    op, field = operation.get("op"), operation.get("field")
    value, previous = operation.get("value"), operation.get("previous_value")
    target = operation.get("target")
    url = target if is_url(target) else page_url
    entry = {"page_label": page_label(url), "page_url": url}

    if op == "set_field" and field in CONTENT_FIELDS:
        card = card_change(previous, value)
        if card == "added":
            return {**entry, "what": "Added a business card for search engines", "before": None, "after": business_card_words(ld_json_blocks(value)), "visible_to_visitors": False, "check_url": card_check_url(url)}
        if card == "removed":
            return {**entry, "what": "Removed the business card for search engines", "before": business_card_words(ld_json_blocks(previous)), "after": None, "visible_to_visitors": False}
        before, after = _changed_part(visible_text(previous) if isinstance(previous, str) else None, visible_text(value) if isinstance(value, str) else None)
        return {**entry, "what": "Changed the text on this page", "before": before, "after": after, "visible_to_visitors": True}
    if op == "set_field":
        before, after = _plain(previous), _plain(value)
        if field == "title":
            what = "Changed the title Google shows for this page" if before else "Added a title Google can show for this page"
            return {**entry, "what": what, "before": before, "after": after, "visible_to_visitors": True}
        if field in DESCRIPTION_FIELDS:
            what = "Changed the description Google shows for this page" if before else "Added a description Google can show for this page"
            return {**entry, "what": what, "before": before, "after": after, "visible_to_visitors": False}
        if field == "h1":
            what = "Changed the main heading on this page" if before else "Added a main heading to this page"
            return {**entry, "what": what, "before": before, "after": after, "visible_to_visitors": True}
        return {**entry, "what": "Changed part of this page", "before": before, "after": after, "visible_to_visitors": True}
    if op == "publish_page":
        title = value.get("title") if isinstance(value, dict) else None
        return {**entry, "page_label": "New page", "what": "Published a new page", "before": None, "after": truncate(title), "visible_to_visitors": True}
    if op == "update_page":
        return {**entry, "what": "Rewrote this page", "before": _page_summary(previous), "after": _page_summary(value), "visible_to_visitors": True}
    if op == "unpublish_page":
        return {**entry, "page_label": "A page we published", "what": "Took down a page we had published", "before": None, "after": None, "visible_to_visitors": True}
    return {**entry, "what": "Changed part of your site", "before": None, "after": None, "visible_to_visitors": True}


def _page_summary(page) -> str | None:
    if not isinstance(page, dict):
        return None
    title = (page.get("title") or "").strip()
    body = visible_text(str(page.get("body_markdown") or "")).strip()
    return truncate(f"{title}: {body}" if title and body else title or body)


def headline_present(change_set, artifact) -> str:
    """What the change does, as an action: the proposal card's own owner title
    when the agent wrote one (content.display.title), else derived from the ops."""
    display = ((artifact.content or {}).get("display") or {}) if artifact is not None else {}
    if isinstance(display, dict) and isinstance(display.get("title"), str) and display["title"].strip():
        return display["title"].strip()
    operations = change_set.operations or []
    targets = {operation.get("target") for operation in operations if is_url(operation.get("target"))}
    page = page_phrase(next(iter(targets))) if len(targets) == 1 else "your site"
    if len(operations) != 1:
        return f"Make {len(operations)} changes to {page}" if operations else f"Update {page}"
    operation = operations[0]
    op, field = operation.get("op"), operation.get("field")
    if op == "publish_page":
        title = (operation.get("value") or {}).get("title") if isinstance(operation.get("value"), dict) else None
        return f"Publish a new page: {title}" if title else "Publish a new page"
    if op == "update_page":
        return f"Rewrite {page}"
    if op == "unpublish_page":
        return "Take down a page we published"
    if field in CONTENT_FIELDS:
        card = card_change(operation.get("previous_value"), operation.get("value"))
        if card == "added":
            return f"Add a business card for search engines to {page}"
        if card == "removed":
            return f"Remove the business card for search engines from {page}"
        return f"Update the text on {page}"
    if field == "title":
        return f"Set the title Google shows for {page}"
    if field in DESCRIPTION_FIELDS:
        return f"Set the description Google shows for {page}"
    if field == "h1":
        return f"Set the main heading on {page}"
    return f"Update {page}"


def change_headline(change_set, artifact, original=None) -> str:
    """The ledger line. Live or undone changes read in the past tense; an undo
    reads 'Undid: added …'; anything not yet applied reads as the action."""
    if original is not None:
        return f"Undid: {_lower_first(past_tense(headline_present(original, artifact)))}"
    present = headline_present(change_set, artifact)
    return past_tense(present) if change_set.status in (STATUS_APPLIED, STATUS_REVERTED) else present


def change_page_url(change_set) -> str | None:
    for operation in change_set.operations or []:
        if is_url(operation.get("target")):
            return operation["target"]
    return None


def ledger_entry(change_set, artifact, original=None) -> dict:
    """The owner-language fields of one change_sets entry. `original` = the
    set this one undoes (None for a forward change)."""
    page_url = change_page_url(original if original is not None else change_set) or change_page_url(change_set)
    approved_by, approved_at = None, None
    if original is not None:
        approved_by, approved_at = "you", change_set.created_at  # only a human Undo click makes a revert
    elif artifact is not None and artifact.status == STATUS_AUTO_APPROVED:
        approved_by, approved_at = "automatic", artifact.reviewed_at or artifact.created_at
    elif artifact is not None and artifact.status == STATUS_APPROVED:
        approved_by, approved_at = "you", artifact.reviewed_at
    return {
        "artifact_id": change_set.artifact_id,
        "headline": change_headline(change_set, artifact, original),
        "page_url": page_url,
        "page_label": page_label(page_url) if page_url else ("New page" if any(operation.get("op") == "publish_page" for operation in change_set.operations or []) else "Your site"),
        "approved_by": approved_by,
        "approved_at": utc_iso(approved_at),
        "is_undo": original is not None,
        "undone_by_change_set_id": change_set.reverted_by_change_set_id,
    }


def change_details(change_set, original=None) -> list[dict]:
    """The plain-words list of what the set changed, one entry per operation."""
    fallback_url = change_page_url(original) if original is not None else None
    return [describe_operation(operation, fallback_url) for operation in change_set.operations or []]


def change_notification_text(change_set, artifact, original=None) -> tuple[str, str]:
    """The Pulse line for a finished apply — names what changed and where."""
    if original is not None:
        page = page_phrase(change_page_url(original))
        return f"Undid the change to {page}", f"{_capitalize(page)} is back the way it was. Tap to see what changed."
    title = change_headline(change_set, artifact)
    invisible = bool(change_set.operations) and all(not describe_operation(operation)["visible_to_visitors"] for operation in change_set.operations)
    body = "Live now. Visitors won't see a difference. Tap to see it." if invisible else "Live now on your site. Tap to see what changed."
    return title, body


CHANGE_NOTICE_BATCH_HOURS = 12  # Marco test #11: one Pulse notice per batch of changes, not one per change


def batched_change_notice(previous_title: str, previous_body: str | None, previous_link_ref: str | None, title: str, change_set_id: str) -> tuple[str, str, str]:
    """Fold one more finished change into a still-unread change notice:
    "We made 3 changes to your site" + one bullet per change, linking to the
    change list. Pure."""
    if (previous_link_ref or "").startswith("changes/"):
        headlines = [line[2:] for line in (previous_body or "").splitlines() if line.startswith("- ")]
        ids = previous_link_ref.split("/", 1)[1].split(",")
    else:
        headlines = [previous_title]
        ids = [(previous_link_ref or "").split("/", 1)[-1]]
    headlines.append(title)
    ids.append(change_set_id)
    return f"We made {len(headlines)} changes to your site", "\n".join(f"- {headline}" for headline in headlines), "changes/" + ",".join(id_ for id_ in ids if id_)


##############################
# DONE FOR YOU (outcomes only)
##############################


def _ref(ref_type: str | None, ref_id: str | None) -> dict:
    return {"type": ref_type, "id": ref_id}


def artifact_outcome(artifact) -> dict | None:
    """One done-for-you item for an artifact, or None when the type is not an
    outcome on its own (fix proposals show up as the live change instead)."""
    if artifact.status in (STATUS_REJECTED, STATUS_SUPERSEDED):
        return None
    content = artifact.content or {}
    evidence = artifact.evidence or {}
    kind, title = None, None
    if artifact.type == "plan":
        kind, title = "plan", "Wrote your plan for the next 3 months"
    elif artifact.type == "draft":
        if content.get("kind") == "refresh":
            kind, title = "draft", f"Wrote a fresher version of {page_phrase(content.get('page'))}"
        else:
            kind, title = "draft", f"Wrote a new page draft: {content['title']}" if content.get("title") else "Wrote a new page draft"
    elif artifact.type == "calendar":
        count = len(content.get("entries") or [])
        kind, title = "calendar", f"Planned {count} new articles for next month" if count else "Planned next month's articles"
    elif artifact.type == "brief":
        kind, title = "brief", "Wrote your weekly update"
    elif artifact.type == "diagnosis":
        kind, title = "report", "Looked into what changed in your numbers"
    elif artifact.type == "pitch":
        where = str(content.get("where") or "")
        host = urlparse(where).netloc.removeprefix("www.") if is_url(where) else ""
        kind, title = "outreach", f"Found a chance to get your business mentioned on {host}" if host else "Found a chance to get your business mentioned"
    elif artifact.type == "reply":
        rating = evidence.get("rating")
        kind, title = "reply", f"Wrote a reply to a {rating}-star review" if isinstance(rating, int) else "Wrote a reply to a review"
    elif artifact.type == "gbp_touches":
        count = len(content.get("posts") or [])
        kind, title = "draft", f"Wrote {count} posts for your Google Business Profile" if count else "Wrote posts for your Google Business Profile"
    elif artifact.type == "review_ask_templates":
        kind, title = "draft", "Wrote messages you can send to ask customers for reviews"
    if kind is None:
        return None
    return {"id": artifact.id, "kind": kind, "title": title, "when": artifact.created_at, "ref": _ref("artifact", artifact.id)}


def change_outcomes(change_sets: list, artifacts_by_id: dict, since: datetime) -> list[dict]:
    """Live fixes, and undone ones marked 'fix_undone' (an undo is never an
    outcome of its own). Dated by when it went live, or when it was undone."""
    reverted_from = revert_links(change_sets)
    by_id = {change_set.id: change_set for change_set in change_sets}
    outcomes = []
    for change_set in change_sets:
        if change_set.id in reverted_from or change_set.applied_at is None:
            continue  # an undo itself, or never went live
        headline = past_tense(headline_present(change_set, artifacts_by_id.get(change_set.artifact_id)))
        undo = by_id.get(change_set.reverted_by_change_set_id) if change_set.reverted_by_change_set_id else None
        if change_set.status == STATUS_REVERTED or undo is not None:
            when = (undo.applied_at or undo.created_at) if undo is not None else change_set.applied_at
            outcomes.append({"id": change_set.id, "kind": "fix_undone", "title": f"Undid: {_lower_first(headline)}", "when": when, "ref": _ref("change_set", change_set.id)})
        elif change_set.status == STATUS_APPLIED:
            outcomes.append({"id": change_set.id, "kind": "fix_live", "title": headline, "when": change_set.applied_at, "ref": _ref("change_set", change_set.id)})
    return [outcome for outcome in outcomes if outcome["when"] and outcome["when"] >= since]


def count_in_window(moments: list, start: datetime | None, end: datetime | None) -> int:
    """How many timestamps fall in [start, end] — what one run produced."""
    if start is None or end is None:
        return 0
    return sum(1 for moment in moments if moment is not None and start <= moment <= end)


def audit_outcome(task, new_findings: int, is_first: bool) -> dict:
    if new_findings == 0:
        title = "Checked your website for problems: nothing new"
    elif is_first:
        title = f"Checked your website for problems: found {new_findings}"
    else:
        title = f"Checked your website for problems: found {new_findings} new"
    return {"id": task.id, "kind": "audit", "title": title, "when": task.finished_at, "ref": _ref("screen", "audit")}


def research_outcome(task, created: int) -> dict | None:
    if task.name == "research_prompts":
        title = f"Found {created} questions your customers ask" if created else "Looked for new questions your customers ask: none new"
        return {"id": task.id, "kind": "research", "title": title, "when": task.finished_at, "ref": _ref("screen", "questions")}
    if task.name == "research_competitors":
        # no count: the list keeps growing as AI answers name new businesses,
        # so "found 12" would contradict the 15 on the home screen
        title = "Found the businesses AI assistants name alongside you" if created else "Checked your competitors: no new ones"
        return {"id": task.id, "kind": "research", "title": title, "when": task.finished_at, "ref": _ref("screen", "home")}
    return None


def report_outcome(report) -> dict:
    period = report.period.strftime("%B %Y") if report.period else ""
    return {"id": report.id, "kind": "report", "title": f"Wrote your monthly report for {period}".strip(), "when": report.published_at, "ref": _ref("report", report.id)}


def done_for_you(items: list[dict | None], since: datetime, limit: int = 30) -> list[dict]:
    """Newest first, inside the window, capped; `when` serialized last."""
    kept = [item for item in items if item is not None and item.get("when") is not None and item["when"] >= since]
    kept.sort(key=lambda item: item["when"], reverse=True)
    return [{**item, "when": utc_iso(item["when"])} for item in kept[:limit]]


##############################
# PLANS (bullets, not prose)
##############################


def first_sentence(text) -> str | None:
    """A bullet's outcome is one sentence; the rest stays behind "Read the reasoning"."""
    if not isinstance(text, str) or not text.strip():
        return None
    return re.split(r"(?<=[.!?])\s+", text.strip(), maxsplit=1)[0]


def plan_workstreams(content: dict | None, needs_you_names: set[str] | None = None) -> list[dict]:
    """A plan's workstreams as a list the screen can render as bullets."""
    needs_you_names = needs_you_names or set()
    rows = []
    for workstream in (content or {}).get("workstreams") or []:
        if not isinstance(workstream, dict) or not workstream.get("name"):
            continue
        name = str(workstream["name"])
        rows.append(
            {
                "title": name,
                "outcome": first_sentence(workstream.get("expected_impact") or workstream.get("why")),
                "first_steps": [str(step) for step in workstream.get("first_moves") or [] if step],
                "needs_you": name in needs_you_names,
            }
        )
    return rows


##############################
# ASK
##############################


def answer_summary(answer: str) -> str:
    """The answer's first line, stripped of markdown — the direct answer."""
    for line in (answer or "").splitlines():
        cleaned = re.sub(r"^\s*(#+|[-*•]|\d+[.)])\s*", "", line)
        cleaned = re.sub(r"[*_`]+", "", cleaned).strip()
        if cleaned:
            return cleaned
    return ""
