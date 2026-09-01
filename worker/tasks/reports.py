"""#16 — the monthly client document (runs on the 1st, covers the previous
calendar month). The assembler fills EVERY number from the database; the AM
agent writes prose over them and never invents one. The result is a reports
row — immutable once published — plus a Pulse notification."""

import json
from datetime import date, datetime, timedelta

from adapters.agents.claude_agent import ClaudeAgent
from objects.brand import Brand
from objects.change_set import ChangeSet
from objects.finding import Finding
from objects.metric_point import MetricPoint
from objects.notification import KIND_UPDATE, Notification
from objects.report import Report
from objects.visibility_run import VisibilityRun
from utils.agent_prompts import ROLE_ACCOUNT_MANAGER
from utils.logger import logger
from utils.task_queue import task_lifecycle
from worker.app import app
from worker.tasks.briefs import completed_task_counts, overdue_client_tasks


@app.task(name="monthly_report", queue="reports", retry=2)
async def monthly_report(task_id: str, brand_id: str) -> None:
    """#16 — assemble the month's facts -> AM narrative over them -> published
    reports row + Pulse. Idempotent: a report already existing for the period
    no-ops, so a retry or double tick never mints a duplicate."""
    async with task_lifecycle(task_id):
        brand = await Brand.get(brand_id)
        period = _previous_month_start()
        if await Report.for_period(brand.id, period) is not None:
            logger.info(f"[reports] brand {brand.id}: report for {period} already exists — nothing to do")
            return

        facts = await _assemble_report_facts(brand, period)
        narrative = await ClaudeAgent().run_role(
            ROLE_ACCOUNT_MANAGER,
            prompt=_report_prompt(brand, period, facts),
            tools=[],
            handlers={},
            operation="monthly_report",
            organization_id=brand.organization_id,
            brand_id=brand.id,
            task_id=task_id,
        )

        report = await Report.create(brand_id=brand.id, period=period, narrative=narrative, metrics=facts, published_at=datetime.now())
        await Notification.create(organization_id=brand.organization_id, kind=KIND_UPDATE, title="Your monthly report is ready", link_ref=f"reports/{report.id}")


REPORT_MAX_WORDS = 600


def _previous_month_start() -> date:
    first_of_this_month = date.today().replace(day=1)
    return (first_of_this_month - timedelta(days=1)).replace(day=1)


##############################
# REPORT FACTS
##############################


async def _assemble_report_facts(brand: Brand, period: date) -> dict:
    """Every number the report may cite — assembled here, never by the agent."""
    period_end = (period + timedelta(days=32)).replace(day=1)

    month_runs = sorted([run for run in await VisibilityRun.latest(brand.id, limit=10) if period <= run.week_of < period_end], key=lambda run: run.week_of)
    visibility: dict | str = "no completed visibility runs this month"
    if month_runs:
        visibility = {
            "weekly_scores": [{"week_of": run.week_of.isoformat(), "overall_score": run.overall_score} for run in month_runs],
            "detection_rate": month_runs[-1].detection_rate,
            "share_of_voice": month_runs[-1].share_of_voice,
            "change_over_month": round((month_runs[-1].overall_score or 0) - (month_runs[0].overall_score or 0), 4),
        }

    open_findings = await Finding.open_for_brand(brand.id)
    resolved_findings = await Finding.resolved_since(brand.id, since=period)
    applied_change_sets = [change_set for change_set in await ChangeSet.for_brand(brand.id) if change_set.applied_at and period <= change_set.applied_at.date() < period_end]

    return {
        "period": period.isoformat(),
        "visibility": visibility,
        "site_health": {
            "open_findings": len(open_findings),
            "resolved_this_month": [{"kind": finding.kind, "summary": finding.summary} for finding in resolved_findings],
        },
        "work_shipped": await completed_task_counts(brand.id, since=datetime(period.year, period.month, 1)),
        "site_changes_applied": [{"applied_at": change_set.applied_at.date().isoformat(), "operations": [f"{operation['op']}:{operation.get('field') or 'page'}" for operation in change_set.operations]} for change_set in applied_change_sets],
        "traffic_and_conversions": await MetricPoint.totals_for_period(brand.id, period, period_end) or "no metrics ingested yet",
        "waiting_on_client": [{"task": client_task.name, "days_waiting": (datetime.now() - client_task.created_at).days} for client_task in await overdue_client_tasks(brand.id)],
    }


def _report_prompt(brand: Brand, period: date, facts: dict) -> str:
    return (
        f"Write the monthly report for the client below covering {period.strftime('%B %Y')}, in markdown,\n"
        f"under {REPORT_MAX_WORDS} words. Structure it: what changed (lead with it, good or bad), what we\n"
        "shipped, what's next, and what (if anything) we need from them. Use ONLY the facts provided — if a\n"
        "section has no data yet, say so in one plain sentence rather than inventing anything.\n\n"
        f"Brand: {brand.name} ({brand.website_url})\n\n"
        f"Facts:\n{json.dumps(facts, indent=2, default=str)}"
    )
