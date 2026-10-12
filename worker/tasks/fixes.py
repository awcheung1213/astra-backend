"""#9 — technical fixes: the site audit's fixable findings -> change_proposal
artifacts behind the approval gate. Two modes, settings.TECHNICAL_FIX_MODE
(agent-tools-plan.md Phase 2):

- "generators" (default): the per-kind $0 code path — a fix map derives the
  operation from the page; anything it can't derive escalates to
  technical_judgment (the Technical agent's prose tail).
- "agent": ONE Technical-agent run per audit over all the run's findings,
  with CMS read tools. It proposes structured operations (propose_change) or
  skips a finding with a reason the client can act on (skip_finding). No
  prose proposals; SEO nuance rides the skills/seo playbooks, not code.

Both modes emit the same artifact shape ({finding_id, kind, operations:
[{field, url, value}]}), so the approval flow and the change-set engine never
know which ran. Writes to client sites are never tools (rule 1): the agent
proposes, a human or preset approves, only the change-set engine applies.

No Pulse notifications here on purpose: a first audit can chain dozens of
fixes at once; the approvals queue is the surface for pending proposals."""

import json
import re
from urllib.parse import urlparse

from sqlalchemy import select

import settings
from adapters.agents.claude_agent import ClaudeAgent
from objects.artifact import STATUS_APPROVED, STATUS_AUTO_APPROVED, STATUS_PENDING_REVIEW, Artifact
from objects.brand import FOOTPRINT_LOCAL, Brand
from objects.finding import STATUS_OPEN, Finding
from objects.site import Site
from utils import clock
from utils.agent_prompts import ROLE_TECHNICAL, skills_for
from utils.approval_flow import fan_out_approval, initial_artifact_status
from utils.database import Database, ReplicaDatabase
from utils.logger import logger
from utils.site_fetch import fetch_page, fetch_page_html
from utils.task_queue import RETRY_WITH_BACKOFF, create_task, task_lifecycle
from worker.app import app
from worker.tasks.changes import ADAPTER_BY_PLATFORM, native_field
from worker.tasks.site_audit import KIND_MISSING_H1, KIND_MISSING_META_DESCRIPTION, KIND_MISSING_SCHEMA, KIND_MISSING_TITLE, page_facts, visible_text

MODE_GENERATORS = "generators"
MODE_AGENT = "agent"

META_DESCRIPTION_MAX_CHARS = 155
MIN_SOURCE_TEXT_CHARS = 40  # a page with less visible text than this can't describe itself
PAGE_EXCERPT_CHARS = 4000  # how much page text rides a prompt / a fetch_url result
# fields a proposal may name when no site is connected (no adapter to ask)
GENERIC_FIELDS = ("title", "h1", "meta_description", "content")


@app.task(name="technical_fix", queue="fixes", retry=RETRY_WITH_BACKOFF)
async def technical_fix(task_id: str, brand_id: str) -> None:
    """#9 — CHAINED, never cron: the site_audit runner emits ONE per audit
    carrying that run's new fixable finding ids (payload.finding_ids; the
    older single finding_id payload still works). Findings resolved or
    dismissed since the audit are skipped — never a proposal for a non-issue."""
    async with task_lifecycle(task_id) as task:
        findings = [finding for finding in [await _open_finding(finding_id) for finding_id in _finding_ids(task.payload)] if finding is not None]
        if not findings:
            logger.info(f"[fixes] task {task_id}: no open findings left — nothing to fix")
            return
        brand = await Brand.get(brand_id)
        if settings.TECHNICAL_FIX_MODE == MODE_AGENT:
            # dedup BEFORE the paid run: a finding with a live proposal or an
            # earlier skip would only be re-bought and thrown away (BUG-049)
            covered_ids = {finding.id for finding in findings if await _existing_proposal(brand.id, finding.id) is not None}
            findings = findings_needing_agent(findings, covered_ids)
            if not findings:
                logger.info(f"[fixes] task {task_id}: every finding already has a live proposal or a skip — no agent run")
                return
            await _run_agent(task_id, brand, findings)
            return
        for finding in findings:
            await _run_generator(task_id, brand, finding)


@app.task(name="technical_judgment", queue="fixes", retry=RETRY_WITH_BACKOFF)
async def technical_judgment(task_id: str, brand_id: str) -> None:
    """Generators mode only — CHAINED from technical_fix when no generator
    can derive the fix (payload carries finding_id). The Technical agent
    reads the finding + the live page and proposes in prose: exact operations,
    expected effect, rollback path. Agent mode never emits this: it proposes
    structured operations or skips with a reason instead."""
    async with task_lifecycle(task_id) as task:
        finding_id = (task.payload or {}).get("finding_id", "")
        finding = await _open_finding(finding_id)
        if finding is None:
            logger.info(f"[fixes] finding {finding_id}: gone or no longer open — no judgment needed")
            return
        brand = await Brand.get(brand_id)
        page_url = (finding.detail or {}).get("url", brand.website_url)
        page_text = visible_text(await fetch_page_html(page_url) or "")

        proposal_markdown = await ClaudeAgent().run_role(
            ROLE_TECHNICAL,
            prompt=_judgment_prompt(brand, finding, page_url, page_text),
            tools=[],
            handlers={},
            operation="technical_judgment",
            organization_id=brand.organization_id,
            brand_id=brand.id,
            task_id=task_id,
        )
        # prose proposals stay human-reviewed under every shipped preset (no
        # macro writes a technical_judgment row); the helper keeps the birth
        # path uniform should a custom row ever exist
        await Artifact.create(
            task_id=task_id,
            brand_id=brand.id,
            type="change_proposal",
            content={"finding_id": finding.id, "kind": finding.kind, "url": page_url, "markdown": proposal_markdown},
            evidence={"finding_summary": finding.summary, "detail": finding.detail},
            status=await initial_artifact_status(brand.id, "technical_judgment", "change_proposal"),
        )


def _finding_ids(payload: dict | None) -> list[str]:
    payload = payload or {}
    if payload.get("finding_ids"):
        return list(payload["finding_ids"])
    return [payload["finding_id"]] if payload.get("finding_id") else []


##############################
# SHARED: proposal birth (both modes)
##############################


async def _create_proposal(task_id: str, brand: Brand, finding: Finding, operations: list[dict], evidence: dict, display: dict | None = None) -> Artifact:
    """One change_proposal per finding, born per the brand's preset — an
    auto-approved one dispatches its change set right here, through the same
    fan-out a human click uses."""
    artifact_status = await initial_artifact_status(brand.id, "technical_fix", "change_proposal")
    artifact = await Artifact.create(
        task_id=task_id,
        brand_id=brand.id,
        type="change_proposal",
        content={"finding_id": finding.id, "kind": finding.kind, "operations": operations, **({"display": display} if display else {})},
        evidence={"finding_summary": finding.summary, **evidence},
        status=artifact_status,
    )
    if artifact_status == STATUS_AUTO_APPROVED:
        await fan_out_approval(artifact)
        logger.info(f"[fixes] finding {finding.id} ({finding.kind}): change_proposal auto-approved by preset — dispatched")
    else:
        logger.info(f"[fixes] finding {finding.id} ({finding.kind}): change_proposal ready for review")
    return artifact


async def _existing_proposal(brand_id: str, finding_id: str) -> Artifact | None:
    """A live proposal already covering this finding (a retried run must not
    put a second one in the queue)."""
    live_statuses = (STATUS_PENDING_REVIEW, STATUS_APPROVED, STATUS_AUTO_APPROVED)
    rows = await ReplicaDatabase.session.execute(select(Artifact).where(Artifact.brand_id == brand_id).where(Artifact.type == "change_proposal").where(Artifact.status.in_(live_statuses)).where(Artifact.content["finding_id"].astext == finding_id))  # type: ignore
    return rows.scalars().first()


def findings_needing_agent(findings: list[Finding], covered_ids: set[str]) -> list[Finding]:
    """The findings worth a paid agent turn: not already covered by a live
    proposal (covered_ids) and not skipped by an earlier run. Pure."""
    return [finding for finding in findings if finding.id not in covered_ids and not (finding.detail or {}).get("skipped")]


async def _open_finding(finding_id: str) -> Finding | None:
    """The task's finding, or None if it vanished or was resolved/dismissed
    between the audit and this run."""
    finding = await ReplicaDatabase.session.get(Finding, finding_id)
    if finding is None or finding.status != STATUS_OPEN:
        return None
    return finding


async def _site_adapter(brand_id: str):
    """The connected site's adapter (its READ primitives are the agent's
    eyes), or None when no site is connected or its platform has no adapter."""
    site = await Site.by_brand(brand_id)
    adapter_class = ADAPTER_BY_PLATFORM.get(site.platform) if site else None
    return adapter_class(site) if adapter_class and site else None


##############################
# AGENT MODE — one run per audit
##############################


class TokenBudgetExceeded(Exception):
    """The run's lifetime token budget (settings.TECHNICAL_FIX_TOKEN_BUDGET) is spent."""


# What the business owner reads on the approval card — written by the agent
# for a reader with zero web or SEO knowledge, stored on the artifact
# (content.display), rendered verbatim by the UI (which holds no copy of its
# own). The schema keeps every field narrow; the gate below refuses the call
# when a technical word slips in, and the agent rewrites.
DISPLAY_BANNED_TERMS = ("meta", "schema", "json", "ld", "json-ld", "excerpt", "markup", "tag", "tags", "block", "gutenberg", "html", "script", "field", "cms", "seo", "crawler", "index", "canonical")
DISPLAY_VERBS = ("Add", "Set", "Update")
DISPLAY_SCHEMA: dict = {
    "type": "object",
    "description": f"The approval card for the business owner, who knows nothing about websites or SEO. Plain everyday words only — these terms are refused: {', '.join(DISPLAY_BANNED_TERMS)}.",
    "required": ["title", "changes"],
    "properties": {
        "title": {"type": "string", "maxLength": 80, "description": "A plain action on a plain page, e.g. 'Add a business card for search engines to your contact page'."},
        "changes": {
            "type": "array",
            "minItems": 1,
            "description": "One entry per operation, in the same order.",
            "items": {
                "type": "object",
                "required": ["verb", "what", "why", "page", "shows"],
                "properties": {
                    "verb": {"type": "string", "enum": list(DISPLAY_VERBS)},
                    "what": {"type": "string", "maxLength": 70, "description": "What this thing is to the owner, e.g. 'the short blurb under your link in search results'."},
                    "why": {"type": "string", "maxLength": 160, "description": "One sentence on what it does for them, e.g. 'Without one, search engines make up their own.'"},
                    "page": {"type": "string", "maxLength": 40, "description": "'your homepage', 'your contact page'."},
                    "shows": {"type": "string", "maxLength": 300, "description": "The exact words a visitor or search engine will see, quoted verbatim from the new value (a name and a one-line description for a business card)."},
                },
            },
        },
    },
}
UNDO_LINE = "You can undo this from Work → Site changes at any time."


def display_errors(display, operation_count: int) -> list[str]:
    """Why a display block is refused: shape, lengths, count mismatch, or a
    technical word in anything but the verbatim `shows` quote. Pure."""
    if not isinstance(display, dict):
        return ["display must be an object"]
    errors: list[str] = []
    title = display.get("title")
    if not isinstance(title, str) or not title.strip():
        errors.append("display.title is required")
    elif len(title) > 80:
        errors.append("display.title must be 80 characters or fewer")
    changes = display.get("changes")
    if not isinstance(changes, list) or not changes:
        return errors + ["display.changes must list one entry per operation"]
    if len(changes) != operation_count:
        errors.append(f"display.changes has {len(changes)} entries for {operation_count} operations")
    limits = {"what": 70, "why": 160, "page": 40, "shows": 300}
    for index, change in enumerate(changes):
        if not isinstance(change, dict):
            errors.append(f"display.changes[{index}] must be an object")
            continue
        if change.get("verb") not in DISPLAY_VERBS:
            errors.append(f"display.changes[{index}].verb must be one of {', '.join(DISPLAY_VERBS)}")
        for key, limit in limits.items():
            value = change.get(key)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"display.changes[{index}].{key} is required")
            elif len(value) > limit:
                errors.append(f"display.changes[{index}].{key} must be {limit} characters or fewer")
    owner_text = " ".join([title if isinstance(title, str) else ""] + [str(change.get(key, "")) for change in changes if isinstance(change, dict) for key in ("what", "why", "page")])
    found = sorted({word for word in re.findall(r"[A-Za-z][A-Za-z-]*", owner_text.lower()) if word in DISPLAY_BANNED_TERMS})
    if found:
        errors.append(f"technical words are not allowed on the owner's card: {', '.join(found)} — say it in everyday words")
    return errors


AGENT_TOOLS: list[dict] = [
    {
        "name": "fetch_url",
        "description": "Fetch a URL on the brand's site the way a browser would. Returns status, content type, title, meta description, H1s, JSON-LD types and the visible text (truncated).",
        "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    },
    {
        "name": "cms_get_page",
        "description": "Read the CMS page behind a live URL with its current raw field values (the values a proposal will overwrite). Use before proposing any content change.",
        "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    },
    {
        "name": "cms_get_settings",
        "description": "Site-level CMS facts: site name, tagline, front-page mode, base URL.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "cms_list_pages",
        "description": "The CMS's published pages (id, url, slug/handle, title) — to find a linking page or check whether a page exists.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "propose_change",
        "description": "Propose the fix for ONE finding as concrete field writes. Each operation sets one writable field of one page on the brand's site to a full new value. Goes to the approval queue; nothing is applied here.",
        "input_schema": {
            "type": "object",
            "properties": {
                "finding_id": {"type": "string"},
                "operations": {"type": "array", "items": {"type": "object", "properties": {"field": {"type": "string"}, "url": {"type": "string"}, "value": {}}, "required": ["field", "url", "value"]}},
                "display": DISPLAY_SCHEMA,
                "technical_note": {"type": "string", "description": "The precise engineering description for the record: field, mechanism, rollback path. Jargon welcome here."},
            },
            "required": ["finding_id", "operations", "display", "technical_note"],
        },
    },
    {
        "name": "skip_finding",
        "description": "Record that a finding cannot be fixed by a page write and say exactly what the client or host must change instead.",
        "input_schema": {"type": "object", "properties": {"finding_id": {"type": "string"}, "reason": {"type": "string"}}, "required": ["finding_id", "reason"]},
    },
]


async def _run_agent(task_id: str, brand: Brand, findings: list[Finding]) -> None:
    site = await Site.by_brand(brand.id)
    adapter = await _site_adapter(brand.id)
    platform = site.platform if site else None
    run = _AgentRun(task_id, brand, findings, platform, adapter)

    spent = 0

    async def on_usage(usage) -> None:
        nonlocal spent
        spent += (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "output_tokens", 0) or 0)
        if spent > settings.TECHNICAL_FIX_TOKEN_BUDGET:
            raise TokenBudgetExceeded(f"{spent} tokens > budget {settings.TECHNICAL_FIX_TOKEN_BUDGET}")

    try:
        await ClaudeAgent().run_role(
            ROLE_TECHNICAL,
            prompt=_agent_prompt(brand, findings, platform, adapter),
            tools=AGENT_TOOLS,
            handlers=run.handlers(),
            operation="technical_fix",
            organization_id=brand.organization_id,
            brand_id=brand.id,
            task_id=task_id,
            on_usage=on_usage,
        )
    except TokenBudgetExceeded as exc:
        # proposals already made are kept; the rest wait for the next audit
        logger.warning(f"[fixes] brand {brand.id}: agent run stopped — {exc}")
    unhandled = [finding.id for finding in findings if finding.id not in run.handled]
    logger.info(f"[fixes] brand {brand.id}: agent run — {len(run.proposed)} proposed, {len(run.skipped)} skipped, {len(unhandled)} unhandled, {spent} tokens")


class _AgentRun:
    """The tool handlers for one run — closures over the run's findings so
    the agent can only act on what it was given, exactly once each."""

    def __init__(self, task_id: str, brand: Brand, findings: list[Finding], platform: str | None, adapter) -> None:
        self.task_id, self.brand, self.platform, self.adapter = task_id, brand, platform, adapter
        self.findings_by_id = {finding.id: finding for finding in findings}
        self.proposed: set[str] = set()
        self.skipped: set[str] = set()

    @property
    def handled(self) -> set[str]:
        return self.proposed | self.skipped

    def handlers(self) -> dict:
        return {"fetch_url": self.fetch_url, "cms_get_page": self.cms_get_page, "cms_get_settings": self.cms_get_settings, "cms_list_pages": self.cms_list_pages, "propose_change": self.propose_change, "skip_finding": self.skip_finding}

    async def fetch_url(self, tool_input: dict) -> str:
        url = tool_input["url"]
        if not same_site(url, self.brand.website_url):
            raise ValueError(f"{url} is not on the brand's site")
        response = await fetch_page(url)
        if response is None:
            return json.dumps({"url": url, "status": "unreachable"})
        html = response.text if "html" in response.headers.get("content-type", "") else ""
        facts = page_facts(html, elapsed_seconds=0.0) if html else {}
        return json.dumps({"url": url, "status": response.status_code, "content_type": response.headers.get("content-type", ""), **facts, "text": visible_text(html)[:PAGE_EXCERPT_CHARS]})

    async def cms_get_page(self, tool_input: dict) -> str:
        return json.dumps(await self._adapter().get_page(tool_input["url"]))

    async def cms_get_settings(self, tool_input: dict) -> str:
        return json.dumps(await self._adapter().get_settings())

    async def cms_list_pages(self, tool_input: dict) -> str:
        return json.dumps(await self._adapter().list_pages())

    async def propose_change(self, tool_input: dict) -> str:
        finding = self._finding(tool_input.get("finding_id", ""))
        errors = validate_operations(self.platform, self.brand.website_url, tool_input.get("operations") or [])
        if errors:
            raise ValueError("; ".join(errors))
        existing = await _existing_proposal(self.brand.id, finding.id)
        if existing is not None:
            self.proposed.add(finding.id)
            return json.dumps({"artifact_id": existing.id, "status": existing.status, "note": "a proposal for this finding already exists"})
        operations = [{"field": operation["field"], "url": operation["url"], "value": operation["value"]} for operation in tool_input["operations"]]
        display_problems = display_errors(tool_input.get("display"), len(operations))
        if display_problems:
            raise ValueError("; ".join(display_problems))
        display = {**tool_input["display"], "undo": UNDO_LINE}
        artifact = await _create_proposal(self.task_id, self.brand, finding, operations, evidence={"technical_note": tool_input.get("technical_note", ""), "derived_from": "Technical agent"}, display=display)
        self.proposed.add(finding.id)
        return json.dumps({"artifact_id": artifact.id, "status": artifact.status})

    async def skip_finding(self, tool_input: dict) -> str:
        finding = self._finding(tool_input.get("finding_id", ""))
        row = await Database.session.get(Finding, finding.id)
        if row is not None:
            row.detail = {**(row.detail or {}), "skipped": {"reason": tool_input.get("reason", ""), "task_id": self.task_id, "at": clock.now().isoformat()}}
            Database.session.add(row)
            await Database.session.flush()
        self.skipped.add(finding.id)
        return json.dumps({"finding_id": finding.id, "skipped": True})

    def _finding(self, finding_id: str) -> Finding:
        finding = self.findings_by_id.get(finding_id)
        if finding is None:
            raise ValueError(f"unknown finding {finding_id!r} — only this run's findings can be handled")
        if finding_id in self.handled:
            raise ValueError(f"finding {finding_id} was already handled in this run")
        return finding

    def _adapter(self):
        if self.adapter is None:
            raise ValueError("no site is connected — CMS tools are unavailable; use fetch_url or skip the finding")
        return self.adapter


def validate_operations(platform: str | None, website_url: str, operations: list[dict]) -> list[str]:
    """Why a proposal is refused at PROPOSAL time (not apply time): empty,
    off-site, or naming a field this CMS cannot write. Pure."""
    errors: list[str] = []
    if not operations:
        return ["at least one operation is required"]
    for index, operation in enumerate(operations):
        field, url, value = operation.get("field"), operation.get("url"), operation.get("value")
        if not field or not url or value in (None, ""):
            errors.append(f"operation {index}: field, url and a non-empty value are required")
            continue
        if not same_site(url, website_url):
            errors.append(f"operation {index}: {url} is not on the brand's site")
        if platform is not None and native_field(platform, field) is None:
            errors.append(f"operation {index}: {platform} has no writable field {field!r}")
        if platform is None and field not in GENERIC_FIELDS:
            errors.append(f"operation {index}: unknown field {field!r} (no site connected; use one of {', '.join(GENERIC_FIELDS)})")
    return errors


def same_site(url: str, website_url: str) -> bool:
    return _host(url) == _host(website_url) and _host(url) != ""


def _host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def _agent_prompt(brand: Brand, findings: list[Finding], platform: str | None, adapter) -> str:
    kinds = {finding.kind for finding in findings}
    lines = [
        "An automated site audit produced the findings below. Handle EVERY finding exactly once: either",
        "propose_change with concrete field writes, or skip_finding with a reason the client can act on.",
        "",
        f"Brand: {brand.name} ({brand.website_url})",
        f"Description: {brand.description or 'n/a'}",
        f"Category: {brand.category or 'n/a'} · Footprint: {brand.footprint or 'n/a'}",
    ]
    if adapter is not None:
        aliases = ", ".join(f"{alias} -> {native}" for alias, native in adapter.FIELD_ALIASES.items())
        lines.append(f"Connected CMS: {platform}. Writable fields: {', '.join(adapter.NATIVE_FIELDS)} (aliases: {aliases}).")
    else:
        lines.append("No site is connected: CMS tools are unavailable; you may still fetch_url. Proposals may name title, h1, meta_description or content.")
    lines += ["", "Findings:"]
    for finding in findings:
        detail = finding.detail or {}
        lines.append(f"- id={finding.id} kind={finding.kind} url={detail.get('url', brand.website_url)}")
        lines.append(f"  {finding.summary}")
        facts = {key: value for key, value in detail.items() if key != "url"}
        if facts:
            lines.append(f"  facts: {json.dumps(facts)[:1500]}")
    lines += [
        "",
        "How to work:",
        "- Read before you write: cms_get_page gives the current raw field values a proposal overwrites; fetch_url gives the rendered page.",
        "- A composite change (a JSON-LD block, a fixed link) is a full new value for the page's content field, composed from the current content you read.",
        "- Group all of a finding's operations into ONE propose_change. Never propose for a finding you were not given.",
        "- Copy in proposals is public and customer-facing: nothing internal, nothing the page does not support.",
        "- The display block is the approval card the business owner reads; they know nothing about websites or SEO. Everyday words only (the tool refuses technical terms). The technical_note is where the engineering goes.",
        "- Finish with two or three sentences summarising what you proposed and what you skipped.",
    ]
    playbooks = skills_for(platform, kinds)
    if playbooks:
        lines += ["", "Playbooks for this run:", "", playbooks]
    return "\n".join(lines)


##############################
# GENERATORS MODE — per-kind $0 code (retired once agent mode is the default)
##############################


async def _run_generator(task_id: str, brand: Brand, finding: Finding) -> None:
    page_url = (finding.detail or {}).get("url", brand.website_url)
    fix = await _generate_fix(finding, brand, page_url)
    if fix is None:
        # the judgment tail — nothing derivable mechanically for this one
        await create_task(brand_id=brand.id, name="technical_judgment", queue="fixes", payload={"finding_id": finding.id})
        logger.info(f"[fixes] finding {finding.id} ({finding.kind}): escalated to technical_judgment")
        return
    await _create_proposal(task_id, brand, finding, [{"field": fix["field"], "url": page_url, "value": fix["value"]}], evidence={"derived_from": fix["derived_from"]})


async def _generate_fix(finding: Finding, brand: Brand, page_url: str) -> dict | None:
    """kind -> proposed operation ({field, value, derived_from}), or None when
    nothing can be derived mechanically (the caller escalates to judgment)."""
    if finding.kind == KIND_MISSING_SCHEMA:
        # a JSON-LD block is a content write: read the current content through
        # the connected site and append the block — no site, no proposal
        adapter = await _site_adapter(brand.id)
        if adapter is None:
            return None
        content_field = adapter.resolve_field("content")
        current_content = (await adapter.get_page(page_url)).get(content_field) or ""
        return {"field": content_field, "value": f'{current_content}\n<script type="application/ld+json">{json.dumps(_schema_markup(brand))}</script>', "derived_from": "brand profile, appended to the page content"}

    page_html = await fetch_page_html(page_url)
    if page_html is None:
        return None

    if finding.kind == KIND_MISSING_TITLE:
        heading = _first_match_text(r"<h1[^>]*>(.*?)</h1>", page_html)
        if not heading:
            return None
        proposed_title = heading if brand.name.lower() in heading.lower() else f"{heading} | {brand.name}"
        return {"field": "title", "value": proposed_title, "derived_from": "the page's <h1>"}

    if finding.kind == KIND_MISSING_H1:
        title = _first_match_text(r"<title[^>]*>(.*?)</title>", page_html)
        return {"field": "h1", "value": title, "derived_from": "the page's <title>"} if title else None

    if finding.kind == KIND_MISSING_META_DESCRIPTION:
        description = _leading_page_text(page_html, META_DESCRIPTION_MAX_CHARS)
        return {"field": "meta_description", "value": description, "derived_from": "the page's opening copy"} if description else None

    return None  # a kind no generator knows -> judgment


def _schema_markup(brand: Brand) -> dict:
    """LocalBusiness/Organization JSON-LD from the brand profile alone (the
    generators-mode heuristic; agent mode picks the type per skills/seo)."""
    business_type = "LocalBusiness" if brand.footprint == FOOTPRINT_LOCAL else "Organization"
    schema = {"@context": "https://schema.org", "@type": business_type, "name": brand.name, "url": brand.website_url}
    if brand.description:
        schema["description"] = brand.description
    return schema


def _first_match_text(pattern: str, html: str) -> str | None:
    match = re.search(pattern, html, re.I | re.S)
    if not match:
        return None
    text = re.sub(r"<[^>]+>", " ", match.group(1))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _leading_page_text(html: str, max_chars: int) -> str | None:
    """The page's opening copy, cut at a word boundary — meta-description raw
    material. None when the page has too little text to describe itself."""
    text = visible_text(html)
    if len(text) < MIN_SOURCE_TEXT_CHARS:
        return None
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0]


def _judgment_prompt(brand: Brand, finding: Finding, page_url: str, page_text: str) -> str:
    return (
        "An automated site audit flagged the issue below and no deterministic fix generator could handle\n"
        "it. Propose the change: the exact operations, the expected effect, and the rollback path. If the\n"
        "safe fix and the optimal fix differ, present both and say which you would ship and why.\n\n"
        f"Brand: {brand.name} ({brand.website_url})\n"
        f"Issue: {finding.kind} — {finding.summary}\n"
        f"Detail: {json.dumps(finding.detail)}\n"
        f"Page: {page_url}\n\n"
        f"Page text (empty if the page is unreachable):\n{page_text[:PAGE_EXCERPT_CHARS]}"
    )
