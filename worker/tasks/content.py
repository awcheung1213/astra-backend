"""The Content agent's runners: the monthly calendar (#10), the chained new
draft (#11), and the chained page refresh (#12).

Facts come from the database (the approved plan, last month's calendar,
visibility gaps, decay evidence, the SME interview); the agent writes over
them. Everything lands behind the approval gate — and the chain is strictly
approval-driven: calendar approval fans out the drafts (api/approvals.py),
never cron."""

import json
from datetime import date, datetime

from adapters.agents.claude_agent import ClaudeAgent
from objects.artifact import STATUS_APPROVED, STATUS_AUTO_APPROVED, STATUS_PENDING_REVIEW, Artifact
from objects.brand import Brand
from objects.notification import KIND_UPDATE, Notification
from objects.prompt import Prompt
from objects.prompt_result import PromptResult
from objects.visibility_run import VisibilityRun
from utils import clock
from utils.agent_json import parse_agent_json
from utils.agent_prompts import ROLE_CONTENT
from utils.approval_flow import fan_out_approval, initial_artifact_status
from utils.logger import logger
from utils.task_queue import RETRY_WITH_BACKOFF, task_lifecycle
from worker.app import app
from worker.tasks.fixes import fetch_page_html, visible_text


@app.task(name="content_calendar", queue="content", retry=RETRY_WITH_BACKOFF)
async def content_calendar(task_id: str, brand_id: str) -> None:
    """#10 — the month's content calendar, drafted plan-aware by the Content
    agent -> calendar artifact behind the approval gate. Approved rows fan out
    content_draft tasks (#11) — from the approval signal, never from cron."""
    async with task_lifecycle(task_id):
        brand = await Brand.get(brand_id)
        live_calendar = await Artifact.latest(brand.id, type="calendar", statuses=[STATUS_PENDING_REVIEW, STATUS_APPROVED, STATUS_AUTO_APPROVED])
        if live_calendar is not None and same_month(live_calendar.created_at, clock.today()):
            # DEC-019: one calendar per brand per month — a re-fire is a no-op (a rejected one may be redrafted)
            logger.info(f"[content_calendar] brand {brand.id}: calendar {live_calendar.id} already drafted this month — skipped")
            return
        facts = await _assemble_calendar_facts(brand)

        agent_output = await ClaudeAgent().run_role(
            ROLE_CONTENT,
            prompt=_calendar_prompt(brand, facts),
            tools=[],
            handlers={},
            operation="content_calendar",
            organization_id=brand.organization_id,
            brand_id=brand.id,
            task_id=task_id,
        )
        entries = _parse_calendar_entries(agent_output)

        artifact_status = await initial_artifact_status(brand.id, "content_calendar", "calendar")
        calendar = await Artifact.create(task_id=task_id, brand_id=brand.id, type="calendar", content={"entries": entries}, evidence=facts, status=artifact_status)
        if artifact_status == STATUS_AUTO_APPROVED:
            # Hands-off: every entry fans out its draft at birth — the human
            # reads the calendar in the weekly digest, not an approval queue
            await fan_out_approval(calendar)
            await Notification.create(organization_id=brand.organization_id, kind=KIND_UPDATE, title="Next month's content calendar is set — drafts are being written", link_ref=f"artifacts/{calendar.id}")
        else:
            await Notification.create(organization_id=brand.organization_id, kind=KIND_UPDATE, title="Next month's content calendar is ready for review", link_ref=f"artifacts/{calendar.id}")


def same_month(created_at: datetime, today: date) -> bool:
    return (created_at.year, created_at.month) == (today.year, today.month)


@app.task(name="content_draft", queue="content", retry=RETRY_WITH_BACKOFF)
async def content_draft(task_id: str, brand_id: str) -> None:
    """#11 — CHAINED from calendar approval, one per approved entry (payload
    carries the entry; #25's data-study write-up rides this runner too, with
    the study material in payload["material"]). The Content agent writes the
    full piece — audience first, target queries in hand, SME-interview
    texture worked in — -> draft artifact behind the gate; approval converts
    it to a change set."""
    async with task_lifecycle(task_id) as task:
        calendar_entry = (task.payload or {}).get("entry", {})
        extra_material = (task.payload or {}).get("material", "")
        if not calendar_entry.get("title"):
            raise ValueError("content_draft payload has no titled entry")
        brand = await Brand.get(brand_id)

        agent_output = await ClaudeAgent().run_role(
            ROLE_CONTENT,
            prompt=_draft_prompt(brand, calendar_entry, extra_material),
            tools=[],
            handlers={},
            operation="content_draft",
            organization_id=brand.organization_id,
            brand_id=brand.id,
            task_id=task_id,
        )
        draft = parse_agent_json(agent_output)
        if not isinstance(draft, dict) or not all(draft.get(field) for field in ("title", "meta_description", "body_markdown")):
            raise ValueError(f"draft output is missing draft fields: {agent_output[:200]}")

        artifact_status = await initial_artifact_status(brand.id, "content_draft", "draft")
        artifact = await Artifact.create(task_id=task_id, brand_id=brand.id, type="draft", content={"kind": "new", **draft}, evidence={"calendar_entry": calendar_entry}, status=artifact_status)
        if artifact_status == STATUS_AUTO_APPROVED:
            await fan_out_approval(artifact)
            logger.info(f"[content] brand {brand.id}: draft auto-approved by preset — dispatched — {draft['title']}")
        else:
            logger.info(f"[content] brand {brand.id}: draft ready for review — {draft['title']}")


@app.task(name="content_refresh", queue="content", retry=RETRY_WITH_BACKOFF)
async def content_refresh(task_id: str, brand_id: str) -> None:
    """#12 — CHAINED from alert_detection, one per decayed page (payload
    carries the page + decay evidence). The Content agent reads the live page
    and proposes the refreshed version -> draft artifact behind the gate;
    approval converts it to a change set. A page that no longer loads is a
    technical problem, not a content one — no-op and let the audit see it."""
    async with task_lifecycle(task_id) as task:
        page_url = (task.payload or {}).get("page", "")
        decay_evidence = (task.payload or {}).get("evidence", {})
        if not page_url:
            raise ValueError("content_refresh payload has no page")
        brand = await Brand.get(brand_id)

        page_text = visible_text(await fetch_page_html(page_url) or "")
        if not page_text:
            logger.info(f"[content] brand {brand.id}: decayed page unreachable ({page_url}) — leaving it to the audit")
            return

        agent_output = await ClaudeAgent().run_role(
            ROLE_CONTENT,
            prompt=_refresh_prompt(brand, page_url, page_text, decay_evidence),
            tools=[],
            handlers={},
            operation="content_refresh",
            organization_id=brand.organization_id,
            brand_id=brand.id,
            task_id=task_id,
        )
        draft = parse_agent_json(agent_output)
        if not isinstance(draft, dict) or not all(draft.get(field) for field in ("title", "meta_description", "body_markdown", "change_summary")):
            raise ValueError(f"refresh output is missing draft fields: {agent_output[:200]}")

        # refreshes overwrite a live page wholesale — no shipped preset autos
        # them (the macro lists content_draft, not content_refresh); the
        # helper keeps the birth path uniform for a future custom row
        artifact_status = await initial_artifact_status(brand.id, "content_refresh", "draft")
        artifact = await Artifact.create(task_id=task_id, brand_id=brand.id, type="draft", content={"kind": "refresh", "page": page_url, **draft}, evidence={"decay": decay_evidence}, status=artifact_status)
        if artifact_status == STATUS_AUTO_APPROVED:
            await fan_out_approval(artifact)
        logger.info(f"[content] brand {brand.id}: refresh drafted for {page_url}")


CALENDAR_MAX_ENTRIES = 8  # pieces per month — quality over volume
GAP_QUERY_LIMIT = 15  # how many visibility-gap queries ride the prompt
REFRESH_PAGE_TEXT_CHARS = 6000  # how much of the live page rides the refresh prompt
DRAFT_INTERVIEW_CHARS = 3000  # how much SME-interview material rides the draft prompt


##############################
# CALENDAR FACTS
##############################


async def _assemble_calendar_facts(brand: Brand) -> dict:
    """Everything the calendar may draw on — assembled here, never by the agent."""
    approved_plan = await Artifact.latest(brand.id, type="plan", statuses=[STATUS_APPROVED, STATUS_AUTO_APPROVED])
    previous_calendar = await Artifact.latest(brand.id, type="calendar")

    latest_runs = await VisibilityRun.latest(brand.id, limit=1)
    visibility: dict | str = "no completed visibility run yet"
    if latest_runs:
        current_run = latest_runs[0]
        visibility = {
            "week_of": current_run.week_of.isoformat(),
            "overall_score": current_run.overall_score,
            "detection_rate": current_run.detection_rate,
            "share_of_voice": current_run.share_of_voice,
        }

    return {
        "current_plan": approved_plan.content if approved_plan else "no approved plan yet",
        "previous_calendar": previous_calendar.content.get("entries") if previous_calendar else "none — this is the first calendar",
        "visibility": visibility,
        "visibility_gaps": await _visibility_gaps(brand.id) or "no collection data yet",
    }


async def _visibility_gaps(brand_id: str) -> list[dict]:
    """Consumer queries where the AI engines don't (fully) mention the brand —
    the calendar's target-query raw material. Rolled up per prompt from the
    newest answer per cell, worst gaps first."""
    prompt_text_by_id = {prompt.id: prompt.text for prompt in await Prompt.by_brand(brand_id)}

    results_by_prompt: dict[str, list[PromptResult]] = {}
    for result in await PromptResult.filter(brand_id):
        results_by_prompt.setdefault(result.prompt_id, []).append(result)

    gaps = []
    for prompt_id, results in results_by_prompt.items():
        query_text = prompt_text_by_id.get(prompt_id)
        if not query_text:
            continue
        mention_rate = sum(result.brand_mentioned for result in results) / len(results)
        if mention_rate < 1.0:
            gaps.append({"query": query_text, "mention_rate": round(mention_rate, 2)})

    gaps.sort(key=lambda gap: gap["mention_rate"])
    return gaps[:GAP_QUERY_LIMIT]


##############################
# AGENT PROMPT + OUTPUT
##############################


def _calendar_prompt(brand: Brand, facts: dict) -> str:
    return (
        f"Draft next month's content calendar for the brand below: up to {CALENDAR_MAX_ENTRIES} pieces across weeks 1-4.\n"
        "Prioritize pieces that close the visibility gaps (queries where AI engines don't mention the brand)\n"
        "and serve the current plan's workstreams. Don't repeat the previous calendar's topics. Use ONLY the\n"
        "facts provided — if there is no plan or no gap data yet, plan from the brand context alone.\n"
        "Schedule only pieces that can be written from these facts: never a placeholder title ([competitor],\n"
        "TBD) and never a piece gated on material the client has not supplied yet.\n\n"
        "Respond with ONLY a JSON array, no prose and no code fences. Each entry:\n"
        '{"week": 1, "title": "...", "target_queries": ["..."], "rationale": "one sentence tied to a fact"}\n\n'
        f"Brand: {brand.name} ({brand.website_url})\n"
        f"Description: {brand.description}\n"
        f"Audience: {brand.audience}\n"
        f"Differentiators: {brand.differentiators}\n\n"
        f"Facts:\n{json.dumps(facts, indent=2, default=str)}"
    )


def _parse_calendar_entries(agent_output: str) -> list[dict]:
    """A JSON array of titled entries, or ValueError so procrastinate retries."""
    entries = parse_agent_json(agent_output)
    if not isinstance(entries, list) or not entries or not all(isinstance(entry, dict) and entry.get("title") for entry in entries):
        raise ValueError(f"calendar output is not a list of titled entries: {agent_output[:200]}")
    return entries


def _draft_prompt(brand: Brand, calendar_entry: dict, extra_material: str) -> str:
    interview_material = json.dumps(brand.interview_raw)[:DRAFT_INTERVIEW_CHARS] if brand.interview_raw else "(no interview material yet)"
    return (
        "Write the piece described by the approved calendar entry below — the full page, ready to publish.\n"
        "Audience first, search/AI engines second, target queries in hand. Work real first-hand details from\n"
        "the SME-interview material into it where they strengthen the piece — that texture is what ranks and\n"
        "gets cited. Never invent facts, stats, case results, or offers.\n\n"
        "Respond with ONLY a JSON object, no prose and no code fences:\n"
        '{"title": "...", "meta_description": "...", "target_queries": ["..."], "body_markdown": "the full piece"}\n\n'
        f"Calendar entry: {json.dumps(calendar_entry)}\n\n"
        f"Brand: {brand.name} ({brand.website_url})\n"
        f"Category: {brand.category}\n"
        f"Description: {brand.description}\n"
        f"Audience: {brand.audience}\n"
        f"Differentiators: {brand.differentiators}\n\n"
        f"SME-interview material:\n{interview_material}\n\n"
        f"Additional material (e.g. an original data study to write up):\n{extra_material or '(none)'}"
    )


def _refresh_prompt(brand: Brand, page_url: str, page_text: str, decay_evidence: dict) -> str:
    return (
        "The page below has been losing search traffic. Refresh it: keep what works, update stale or thin\n"
        "sections, strengthen it against the queries it should serve, and improve structure. Work ONLY from\n"
        "the current page content and the brand context — never invent facts, stats, or offers.\n\n"
        "Respond with ONLY a JSON object, no prose and no code fences:\n"
        '{"title": "...", "meta_description": "...", "target_queries": ["..."], "body_markdown": "the full refreshed page",\n'
        ' "change_summary": "what you changed and why, for the reviewer"}\n\n'
        f"Brand: {brand.name} ({brand.website_url})\n"
        f"Description: {brand.description}\n"
        f"Audience: {brand.audience}\n"
        f"Differentiators: {brand.differentiators}\n\n"
        f"Page: {page_url}\n"
        f"Decay evidence: {json.dumps(decay_evidence)}\n\n"
        f"Current page text:\n{page_text[:REFRESH_PAGE_TEXT_CHARS]}"
    )
