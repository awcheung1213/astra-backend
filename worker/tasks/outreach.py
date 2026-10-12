"""The Outreach scout (deliverables #13, #20, #21, #22): one scout, four
hunting grounds, selected by payload["scan"] (schedules.py fans them out on
their own cadences). The scout FINDS and DRAFTS — it never sends: every
opportunity lands as a pitch artifact routed to the human sender, and that
boundary is the design, not a limitation."""

from urllib.parse import urlparse

from adapters.agents.claude_agent import ClaudeAgent
from adapters.services.claude_service import ClaudeService
from objects.artifact import STATUS_APPROVED, STATUS_PENDING_REVIEW, Artifact
from objects.brand import Brand
from objects.notification import KIND_UPDATE, URGENCY_URGENT, Notification
from utils.agent_json import parse_agent_json
from utils.agent_prompts import ROLE_OUTREACH_SCOUT
from utils.logger import logger
from utils.task_queue import RETRY_WITH_BACKOFF, task_lifecycle
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
OPPORTUNITY_LIMIT = 5  # per run — five good pitches beat twenty mediocre ones

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
        "correct answer when you find nothing genuinely worth a human's sending time.\n"
        "Respond with ONLY a JSON array, no prose and no code fences. Each entry:\n"
        '{"where": "url or venue", "why_fit": "why this brand is credible here", "pitch": "the draft, in plain human language",\n'
        ' "talking_points": ["..."], "deadline": "when stated, else omit"}\n\n'
        f"Brand: {brand.name} ({brand.website_url})\n"
        f"Category: {brand.category}\n"
        f"Description: {brand.description}\n"
        f"Audience: {brand.audience}\n"
        f"Differentiators: {brand.differentiators}\n"
        f"Footprint: {brand.footprint}"
    )
