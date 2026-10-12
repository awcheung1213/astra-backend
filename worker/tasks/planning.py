"""The backward-looking "why" and the forward-looking "what next"
(deliverables #23, #24/#8): the Analyst's diagnosis and the Strategist's
plan. Both write over assembler-supplied facts — the numbers come from the
database, never the agent."""

import json
from datetime import timedelta

from adapters.agents.claude_agent import ClaudeAgent
from objects.artifact import STATUS_APPROVED, STATUS_AUTO_APPROVED, Artifact
from objects.brand import Brand
from objects.change_set import ChangeSet
from objects.competitor import Competitor
from objects.finding import Finding
from objects.goal import Goal
from objects.metric_point import MetricPoint
from objects.notification import KIND_UPDATE, Notification
from objects.visibility_run import VisibilityRun
from utils import clock
from utils.agent_json import parse_agent_json
from utils.agent_prompts import ROLE_ANALYST, ROLE_STRATEGIST
from utils.task_queue import RETRY_WITH_BACKOFF, task_lifecycle
from worker.app import app
from worker.tasks.briefs import change_set_outcome, completed_task_counts, work_facts


@app.task(name="plan_update", queue="planning", retry=RETRY_WITH_BACKOFF)
async def plan_update(task_id: str, brand_id: str) -> None:
    """#24 (quarterly trigger) / #8 (first run, fired by onboarding) — the
    Strategist: goals + the latest diagnosis + the quarter's trajectory ->
    a few named workstreams, sequenced, with an explicit stop-doing list.
    Lands as a plan artifact behind the gate; the approval endpoint fans
    approved workstreams out (content emphasis, #25 client data tasks,
    #4 re-run) and the calendar plans against it every month."""
    async with task_lifecycle(task_id):
        brand = await Brand.get(brand_id)
        facts = await _assemble_plan_facts(brand)

        agent_output = await ClaudeAgent().run_role(
            ROLE_STRATEGIST,
            prompt=_plan_prompt(brand, facts),
            tools=[],
            handlers={},
            operation="plan_update",
            organization_id=brand.organization_id,
            brand_id=brand.id,
            task_id=task_id,
        )
        plan = parse_agent_json(agent_output)
        workstreams_valid = isinstance(plan, dict) and isinstance(plan.get("workstreams"), list) and plan["workstreams"] and all(isinstance(workstream, dict) and workstream.get("name") and workstream.get("why") for workstream in plan["workstreams"])
        if not workstreams_valid or not plan.get("summary_markdown"):
            raise ValueError(f"plan output is missing summary or named workstreams: {agent_output[:200]}")

        artifact = await Artifact.create(task_id=task_id, brand_id=brand.id, type="plan", content=plan, evidence=facts)
        await Artifact.supersede_pending(brand.id, type="plan", keep_id=artifact.id)  # DEC-018: one roadmap waits for review, the newest
        await Notification.create(organization_id=brand.organization_id, kind=KIND_UPDATE, title="Your new roadmap is ready for review", link_ref=f"artifacts/{artifact.id}")


@app.task(name="diagnosis", queue="planning", retry=RETRY_WITH_BACKOFF)
async def diagnosis(task_id: str, brand_id: str) -> None:
    """#23 — CHAINED from alert_detection on an update-shaped drop (payload
    carries the trigger + its evidence; the Ask endpoint will emit these too).
    The Analyst reads the assembled timeline and produces a diagnosis WITH
    RECEIPTS — every causal claim tied to a dated data point. Auto-approved
    (informational, read-only) and delivered to Pulse: the detector's alert
    said "we're investigating"; this is the answer."""
    async with task_lifecycle(task_id) as task:
        trigger = (task.payload or {}).get("trigger", "unspecified")
        trigger_evidence = (task.payload or {}).get("evidence", {})
        brand = await Brand.get(brand_id)
        facts = await assemble_diagnosis_facts(brand, trigger, trigger_evidence)

        diagnosis_markdown = await ClaudeAgent().run_role(
            ROLE_ANALYST,
            prompt=_diagnosis_prompt(brand, facts),
            tools=[],
            handlers={},
            operation="diagnosis",
            organization_id=brand.organization_id,
            brand_id=brand.id,
            task_id=task_id,
        )

        artifact = await Artifact.create(task_id=task_id, brand_id=brand.id, type="diagnosis", content={"trigger": trigger, "markdown": diagnosis_markdown}, evidence=facts, status=STATUS_AUTO_APPROVED)
        await Notification.create(organization_id=brand.organization_id, kind=KIND_UPDATE, title="What happened — our diagnosis", link_ref=f"artifacts/{artifact.id}")


DIAGNOSIS_MAX_WORDS = 400
TIMELINE_WEEKS = 6  # how far back the weekly metric timeline reaches
RECENT_WINDOW_DAYS = 30  # "recent" for shipped work + resolved findings

PLAN_MAX_WORKSTREAMS = 4  # a roadmap that only adds is not a strategy
PLAN_TREND_WEEKS = 12  # a quarter of visibility trend points
QUARTER_DAYS = 90


##############################
# PLAN FACTS
##############################


async def _assemble_plan_facts(brand: Brand) -> dict:
    """Everything the roadmap may steer by — assembled here, never by the agent."""
    today = clock.today()
    quarter_start = today - timedelta(days=QUARTER_DAYS)
    quarter_start_at = clock.now() - timedelta(days=QUARTER_DAYS)

    latest_diagnosis = await Artifact.latest(brand.id, type="diagnosis")
    current_plan = await Artifact.latest(brand.id, type="plan", statuses=[STATUS_APPROVED, STATUS_AUTO_APPROVED])
    open_findings = await Finding.open_for_brand(brand.id)

    open_finding_counts_by_kind: dict[str, int] = {}
    for finding in open_findings:
        open_finding_counts_by_kind[finding.kind] = open_finding_counts_by_kind.get(finding.kind, 0) + 1

    return {
        "goals": [{"metric": goal.metric, "target": float(goal.target), "deadline": goal.deadline.isoformat() if goal.deadline else None} for goal in await Goal.active(brand.id)] or "no goals set yet",
        "latest_diagnosis": latest_diagnosis.content.get("markdown") if latest_diagnosis else "none yet",
        "current_plan": current_plan.content if current_plan else "none — this is the first plan",
        "visibility_trend": [{"week_of": run.week_of.isoformat(), "overall_score": run.overall_score} for run in reversed(await VisibilityRun.latest(brand.id, limit=PLAN_TREND_WEEKS))],
        "quarter_metrics": await MetricPoint.totals_for_period(brand.id, quarter_start, today) or "no metrics ingested yet",
        "site_health": {"open_findings_by_kind": open_finding_counts_by_kind},
        # the tracked rivals by name — without them a "[competitor] alternative"
        # workstream is a placeholder the calendar and drafts inherit (e2e 2026-09-15, gap 5)
        "competitors": [competitor.name for competitor in await Competitor.get_competitors_by_brand_id(brand.id)] or "none tracked yet",
        "work_this_quarter": await work_facts(brand.id, await completed_task_counts(brand.id, since=quarter_start_at), await ChangeSet.for_brand(brand.id), since=quarter_start_at),
    }


def _plan_prompt(brand: Brand, facts: dict) -> str:
    return (
        f"Write the brand's roadmap for the next quarter: at most {PLAN_MAX_WORKSTREAMS} named workstreams,\n"
        "sequenced (what happens first, what waits), each tied to the evidence in the facts. Revise the\n"
        "current plan rather than starting over — keep what is working, and be explicit about what to STOP.\n"
        "Use ONLY the facts provided; where data is missing, plan from brand context and say so.\n"
        "Every string is read by the business owner on their Roadmap page: write in a calm, plain voice —\n"
        "no capitalised shouting (write 'Stop', never 'STOP'), no internal labels such as 'WS1' or 'WS4'\n"
        "(name the workstream instead), and each first_moves / stop_doing entry is one short, self-contained item.\n\n"
        "Respond with ONLY a JSON object, no prose and no code fences:\n"
        '{"summary_markdown": "the plan narrative, for the owner",\n'
        ' "workstreams": [{"name": "...", "why": "tied to a fact", "expected_impact": "...", "effort": "low|medium|high", "first_moves": ["..."],\n'
        '                  "needs_client_data": true ONLY when the workstream depends on data only the client can collect (e.g. an original study)}],\n'
        ' "stop_doing": ["..."]}\n\n'
        f"Brand: {brand.name} ({brand.website_url})\n"
        f"Category: {brand.category}\n"
        f"Description: {brand.description}\n"
        f"Audience: {brand.audience}\n"
        f"Differentiators: {brand.differentiators}\n"
        f"Footprint: {brand.footprint}\n\n"
        f"Facts:\n{json.dumps(facts, indent=2, default=str)}"
    )


##############################
# DIAGNOSIS FACTS
##############################


async def assemble_diagnosis_facts(brand: Brand, trigger: str, trigger_evidence: dict) -> dict:
    """The timeline the Analyst reasons over — assembled here, never by the
    agent. Everything is dated so causal claims can carry receipts."""
    today = clock.today()
    recent_cutoff = clock.now() - timedelta(days=RECENT_WINDOW_DAYS)

    weekly_metrics = []
    for weeks_back in range(TIMELINE_WEEKS, 0, -1):
        week_start = today - timedelta(days=7 * weeks_back)
        totals = await MetricPoint.totals_for_period(brand.id, week_start, week_start + timedelta(days=7))
        weekly_metrics.append({"week_start": week_start.isoformat(), "totals": totals})

    open_findings = await Finding.open_for_brand(brand.id)
    resolved_findings = await Finding.resolved_since(brand.id, since=today - timedelta(days=RECENT_WINDOW_DAYS))

    # "what did WE change right before the drop" is the diagnosis's sharpest question;
    # each entry says whether it is still live, was undone, or IS an undo (BUG-050)
    change_sets = await ChangeSet.for_brand(brand.id)
    revert_set_ids = {change_set.reverted_by_change_set_id for change_set in change_sets if change_set.reverted_by_change_set_id}
    changes_applied = [
        {"applied_at": change_set.applied_at.isoformat(), "what": change_set_outcome(change_set, revert_set_ids), "operations": [{"op": operation["op"], "field": operation.get("field"), "target": operation.get("target")} for operation in change_set.operations]}
        for change_set in change_sets
        if change_set.applied_at and change_set.applied_at > recent_cutoff
    ]
    return {
        "trigger": {"kind": trigger, "evidence": trigger_evidence},
        "weekly_metrics": weekly_metrics,
        "visibility_trend": [{"week_of": run.week_of.isoformat(), "overall_score": run.overall_score} for run in reversed(await VisibilityRun.latest(brand.id, limit=TIMELINE_WEEKS))],
        "site_health": {
            "open_findings": [{"kind": finding.kind, "summary": finding.summary, "since": finding.created_at.isoformat() if finding.created_at else None} for finding in open_findings],
            "resolved_recently": [{"kind": finding.kind, "summary": finding.summary} for finding in resolved_findings],
        },
        "changes_applied_recently": changes_applied,
        "work_recently": await work_facts(brand.id, await completed_task_counts(brand.id, since=recent_cutoff), change_sets, since=recent_cutoff),
    }


def _diagnosis_prompt(brand: Brand, facts: dict) -> str:
    return (
        f"Diagnose what happened for the brand below, in markdown, under {DIAGNOSIS_MAX_WORDS} words.\n"
        "Rank the candidate explanations by evidence; tie every causal claim to a dated data point from the\n"
        "facts; say which explanation you believe, how confident you are, and what data would settle it.\n"
        "An honest 'the data cannot distinguish these causes' beats a confident story. Use ONLY the facts\n"
        "provided. Written for the business owner AND the strategist who plans from it — plain language,\n"
        "no jargon.\n\n"
        f"Brand: {brand.name} ({brand.website_url})\n\n"
        f"Facts:\n{json.dumps(facts, indent=2, default=str)}"
    )
