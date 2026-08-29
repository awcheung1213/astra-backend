"""#9's two tiers: deterministic fix generators + the judgment tail.

technical_fix is tier 1 — $0 code: finding-kind -> generator -> a structured
change_proposal artifact behind the approval gate (approval converts it to a
change set; only the change-set engine ever touches the site). Whatever a
generator can't derive mechanically escalates to technical_judgment, the
Technical agent's tier — its proposals are prose (exact operations, expected
effect, rollback path) written for a human first.

No Pulse notifications here on purpose: a first audit can chain dozens of
fixes at once; the approvals queue is the surface for pending proposals."""

import json
import re

import httpx

from adapters.agents.claude_agent import ClaudeAgent
from objects.artifact import Artifact
from objects.brand import FOOTPRINT_LOCAL, Brand
from objects.finding import STATUS_OPEN, Finding
from utils.agent_prompts import ROLE_TECHNICAL
from utils.database import ReplicaDatabase
from utils.logger import logger
from utils.task_queue import create_task, task_lifecycle
from worker.app import app
from worker.tasks.site_audit import FETCH_TIMEOUT_SECONDS, KIND_MISSING_H1, KIND_MISSING_META_DESCRIPTION, KIND_MISSING_SCHEMA, KIND_MISSING_TITLE


@app.task(name="technical_fix", queue="fixes", retry=2)
async def technical_fix(task_id: str, brand_id: str) -> None:
    """#9 tier 1 — fix generators: finding-kind -> fix map (schema, metas,
    titles). CHAINED, never cron: produced by the site_audit runner, one per
    new fixable finding (payload carries finding_id). Emits a structured
    change_proposal artifact behind the approval gate; whatever no generator
    can derive escalates to technical_judgment."""
    async with task_lifecycle(task_id) as task:
        finding_id = (task.payload or {}).get("finding_id", "")
        finding = await _open_finding(finding_id)
        if finding is None:
            logger.info(f"[fixes] finding {finding_id}: gone or no longer open — nothing to fix")
            return
        brand = await Brand.get(brand_id)
        page_url = (finding.detail or {}).get("url", brand.website_url)

        fix = await _generate_fix(finding, brand, page_url)
        if fix is None:
            # the judgment tail — nothing derivable mechanically for this one
            await create_task(brand_id=brand_id, name="technical_judgment", queue="fixes", payload={"finding_id": finding.id})
            logger.info(f"[fixes] finding {finding.id} ({finding.kind}): escalated to technical_judgment")
            return

        await Artifact.create(
            task_id=task_id,
            brand_id=brand.id,
            type="change_proposal",
            content={"finding_id": finding.id, "kind": finding.kind, "operations": [{"field": fix["field"], "url": page_url, "value": fix["value"]}]},
            evidence={"finding_summary": finding.summary, "derived_from": fix["derived_from"]},
        )
        logger.info(f"[fixes] finding {finding.id} ({finding.kind}): change_proposal ready for review")


@app.task(name="technical_judgment", queue="fixes", retry=2)
async def technical_judgment(task_id: str, brand_id: str) -> None:
    """#9's judgment tail — CHAINED from technical_fix when no generator can
    derive the fix (payload carries finding_id). The Technical agent reads the
    finding + the live page and proposes: exact operations, expected effect,
    rollback path -> prose change_proposal artifact behind the approval gate."""
    async with task_lifecycle(task_id) as task:
        finding_id = (task.payload or {}).get("finding_id", "")
        finding = await _open_finding(finding_id)
        if finding is None:
            logger.info(f"[fixes] finding {finding_id}: gone or no longer open — no judgment needed")
            return
        brand = await Brand.get(brand_id)
        page_url = (finding.detail or {}).get("url", brand.website_url)
        page_text = _visible_text(await _fetch_page_html(page_url) or "")

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
        await Artifact.create(
            task_id=task_id,
            brand_id=brand.id,
            type="change_proposal",
            content={"finding_id": finding.id, "kind": finding.kind, "url": page_url, "markdown": proposal_markdown},
            evidence={"finding_summary": finding.summary, "detail": finding.detail},
        )


META_DESCRIPTION_MAX_CHARS = 155
MIN_SOURCE_TEXT_CHARS = 40  # a page with less visible text than this can't describe itself
PAGE_EXCERPT_CHARS = 4000  # how much page text rides the judgment prompt


##############################
# FIX GENERATORS (tier 1 — deterministic, $0)
##############################


async def _generate_fix(finding: Finding, brand: Brand, page_url: str) -> dict | None:
    """kind -> proposed operation ({field, value, derived_from}), or None when
    nothing can be derived mechanically (the caller escalates to judgment)."""
    if finding.kind == KIND_MISSING_SCHEMA:
        return {"field": "schema_markup", "value": _schema_markup(brand), "derived_from": "brand profile"}

    page_html = await _fetch_page_html(page_url)
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
    """LocalBusiness/Organization JSON-LD from the brand profile alone."""
    business_type = "LocalBusiness" if brand.footprint == FOOTPRINT_LOCAL else "Organization"
    schema = {"@context": "https://schema.org", "@type": business_type, "name": brand.name, "url": brand.website_url}
    if brand.description:
        schema["description"] = brand.description
    return schema


##############################
# PAGE HELPERS
##############################


async def _fetch_page_html(url: str) -> str | None:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=FETCH_TIMEOUT_SECONDS, headers={"User-Agent": "AstraAudit/1.0"}) as client:
            response = await client.get(url)
    except httpx.HTTPError:
        return None
    if response.status_code >= 400:
        return None
    return response.text


def _first_match_text(pattern: str, html: str) -> str | None:
    match = re.search(pattern, html, re.I | re.S)
    if not match:
        return None
    text = re.sub(r"<[^>]+>", " ", match.group(1))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _visible_text(html: str) -> str:
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
    body = re.sub(r"<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", body).strip()


def _leading_page_text(html: str, max_chars: int) -> str | None:
    """The page's opening copy, cut at a word boundary — meta-description raw
    material. None when the page has too little text to describe itself."""
    text = _visible_text(html)
    if len(text) < MIN_SOURCE_TEXT_CHARS:
        return None
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0]


##############################
# JUDGMENT (tier 2 — Technical agent)
##############################


async def _open_finding(finding_id: str) -> Finding | None:
    """The task's finding, or None if it vanished or was resolved/dismissed
    between the audit and this run — both tiers no-op instead of proposing a
    fix for a non-issue."""
    finding = await ReplicaDatabase.session.get(Finding, finding_id)
    if finding is None or finding.status != STATUS_OPEN:
        return None
    return finding


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
