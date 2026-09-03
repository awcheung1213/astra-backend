"""The FE's read layer — one GET per mockup surface, deliberately simple:
plain dict responses (no schemas file), each shaped for its page so the FE
renders without stitching. Screens -> endpoints:

  Results (home)          GET /brands/{id}/overview
  Tracked questions       GET /brands/{id}/questions
  Work (+audit/activity/  GET /brands/{id}/worklog
    changes sub-pages)
  Your tasks              GET /brands/{id}/approvals   (+ GET /artifacts/{id} detail)
  Roadmap                 GET /brands/{id}/roadmap
  Pulse                   GET /organizations/{id}/pulse
  Reports archive/detail  GET /brands/{id}/reports · GET /reports/{id}
  Leads                   GET /brands/{id}/leads

Deliberately not served: per-city score scoping (dropped 2026-08-30 —
framework v1 scores brand-level) and a computed site-health score (counts
only, no invented formula). Auth rides the router-level guard
(utils/authz.py); GET /brands scopes by the caller's org itself."""

from datetime import date, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request, status

from objects.artifact import STATUS_APPROVED, STATUS_AUTO_APPROVED, Artifact
from objects.brand import Brand
from objects.change_set import ChangeSet
from objects.competitor import Competitor
from objects.competitor_mention import CompetitorMention
from objects.finding import Finding
from objects.goal import Goal
from objects.lead import Lead
from objects.metric_point import MetricPoint
from objects.notification import Notification
from objects.prompt import Prompt
from objects.prompt_result import PromptResult
from objects.report import Report
from objects.task import STATUS_DONE, STATUS_QUEUED, STATUS_RUNNING, Task
from objects.visibility_run import VisibilityRun
from utils.authz import current_user
from worker.tasks.briefs import CLIENT_TASK_NAMES

router = APIRouter(prefix="/api/v1")

TREND_WEEKS = 13  # ~a quarter of weekly points for the hero chart
LEDGER_WINDOW_DAYS = 31  # "shipped this month"


@router.get("/brands")
async def brands(request: Request) -> dict:
    """Brand discovery for the FE — the signed-in caller's org's brands
    (empty before signup completes)."""
    user = current_user(request)
    org_brands = await Brand.by_organization_id(user.organization_id) if user.organization_id else []
    return {
        "brands": [
            {
                "id": brand.id,
                "name": brand.name,
                "website_url": brand.website_url,
                "footprint": brand.footprint,
                "organization_id": brand.organization_id,
                "platform": brand.platform,
                "description": brand.description,
                "category": brand.category,
                "audience": brand.audience,
                "differentiators": brand.differentiators,
            }
            for brand in org_brands
        ]
    }


@router.get("/brands/{brand_id}/overview")
async def overview(brand_id: str) -> dict:
    """The Results page: brief, score + trend + components, share of voice,
    and the month's metric tiles (this month vs last)."""
    brand = await Brand.get(brand_id)
    latest_brief = await Artifact.latest(brand.id, type="brief")

    runs = await VisibilityRun.latest(brand.id, limit=TREND_WEEKS)
    current_run = runs[0] if runs else None
    visibility = None
    share_of_voice = None
    if current_run:
        visibility = {
            "week_of": current_run.week_of.isoformat(),
            "overall_score": current_run.overall_score,
            "components": {
                "detection_rate": current_run.detection_rate,
                "share_of_voice": current_run.share_of_voice,
                "positive_rate": current_run.positive_rate,
                "top3_rate": current_run.top3_rate,
                "citation_rate": current_run.citation_rate,
            },
            "trend": [{"week_of": run.week_of.isoformat(), "overall_score": run.overall_score} for run in reversed(runs)],
        }
        share_of_voice = {**await PromptResult.counts_for_run(current_run.id), "competitor_mentions": await CompetitorMention.counts_for_run(current_run.id)}

    first_of_this_month = date.today().replace(day=1)
    first_of_last_month = (first_of_this_month - timedelta(days=1)).replace(day=1)
    return {
        "brand": {"id": brand.id, "name": brand.name, "website_url": brand.website_url, "footprint": brand.footprint, "platform": brand.platform, "description": brand.description, "category": brand.category, "audience": brand.audience, "differentiators": brand.differentiators},
        "competitors": [competitor.name for competitor in await Competitor.get_competitors_by_brand_id(brand.id)],
        "brief": {"markdown": latest_brief.content.get("markdown"), "created_at": latest_brief.created_at.isoformat()} if latest_brief else None,
        "visibility": visibility,
        "share_of_voice": share_of_voice,
        "metrics": {
            "this_month": await MetricPoint.totals_for_period(brand.id, first_of_this_month, date.today() + timedelta(days=1)),
            "last_month": await MetricPoint.totals_for_period(brand.id, first_of_last_month, first_of_this_month),
        },
    }


@router.get("/brands/{brand_id}/questions")
async def questions(brand_id: str) -> dict:
    """The tracked-questions page: every non-retired prompt with its
    last-week stats (asked count, mention rate, average rank when ranked)."""
    await Brand.get(brand_id)
    results_by_prompt: dict[str, list[PromptResult]] = {}
    for result in await PromptResult.filter(brand_id):
        results_by_prompt.setdefault(result.prompt_id, []).append(result)

    question_rows = []
    for prompt in await Prompt.by_brand(brand_id):
        results = results_by_prompt.get(prompt.id, [])
        ranks = [result.rank for result in results if result.rank is not None]
        question_rows.append(
            {
                "id": prompt.id,
                "text": prompt.text,
                "category": prompt.category,
                "is_active": prompt.is_active,
                "source": prompt.source,
                "asked": len(results),
                "mention_rate": round(sum(result.brand_mentioned for result in results) / len(results), 2) if results else None,
                "average_rank": round(sum(ranks) / len(ranks), 1) if ranks else None,
            }
        )
    return {"questions": question_rows}


@router.get("/brands/{brand_id}/worklog")
async def worklog(brand_id: str) -> dict:
    """The Work page + its audit/activity/changes sub-pages: site health,
    what's running/queued, the shipped ledger, and applied change sets."""
    await Brand.get(brand_id)
    month_ago = datetime.now() - timedelta(days=LEDGER_WINDOW_DAYS)

    running_and_queued = await Task.for_brand(brand_id, statuses=[STATUS_RUNNING, STATUS_QUEUED])
    machine_tasks = [task for task in running_and_queued if task.name not in CLIENT_TASK_NAMES]
    done_tasks = [task for task in await Task.for_brand(brand_id, statuses=[STATUS_DONE]) if task.finished_at and task.finished_at > month_ago]

    return {
        "site_health": {
            "open": [{"kind": finding.kind, "severity": finding.severity, "summary": finding.summary, "found_at": finding.created_at.isoformat() if finding.created_at else None} for finding in await Finding.open_for_brand(brand_id)],
            "resolved_recently": [{"kind": finding.kind, "summary": finding.summary, "resolved_at": finding.resolved_at.isoformat() if finding.resolved_at else None} for finding in await Finding.resolved_since(brand_id, since=date.today() - timedelta(days=LEDGER_WINDOW_DAYS))],
        },
        "running": [{"id": task.id, "name": task.name, "started": task.created_at.isoformat()} for task in machine_tasks if task.status == STATUS_RUNNING],
        "queued": [{"id": task.id, "name": task.name} for task in machine_tasks if task.status == STATUS_QUEUED],
        "shipped": [{"id": task.id, "name": task.name, "finished_at": task.finished_at.isoformat()} for task in done_tasks],
        "change_sets": [
            {"id": change_set.id, "status": change_set.status, "operation_count": len(change_set.operations), "applied_at": change_set.applied_at.isoformat() if change_set.applied_at else None, "reverted_by": change_set.reverted_by_change_set_id} for change_set in await ChangeSet.for_brand(brand_id)
        ],
    }


@router.get("/brands/{brand_id}/approvals")
async def approvals(brand_id: str) -> dict:
    """The Your-tasks page: the approvals lane (pending artifacts, oldest
    first) and the we-need-from-you lane (open client-contribution tasks)."""
    await Brand.get(brand_id)
    client_tasks = [task for task in await Task.for_brand(brand_id, statuses=[STATUS_QUEUED]) if task.name in CLIENT_TASK_NAMES]
    return {
        "pending": [{"id": artifact.id, "type": artifact.type, "created_at": artifact.created_at.isoformat(), "content": artifact.content} for artifact in await Artifact.pending_for_brand(brand_id)],
        "waiting_on_you": [{"id": task.id, "name": task.name, "days_waiting": (datetime.now() - task.created_at).days if task.created_at else 0} for task in client_tasks],
    }


@router.get("/artifacts/{artifact_id}")
async def artifact_detail(artifact_id: str) -> dict:
    """Full artifact for detail views ('Read full draft', Pulse links)."""
    artifact = await Artifact.get(artifact_id)
    return {
        "id": artifact.id,
        "brand_id": artifact.brand_id,
        "type": artifact.type,
        "status": artifact.status,
        "content": artifact.content,
        "evidence": artifact.evidence,
        "created_at": artifact.created_at.isoformat(),
        "reviewed_by": artifact.reviewed_by,
        "reviewed_at": artifact.reviewed_at.isoformat() if artifact.reviewed_at else None,
    }


@router.get("/brands/{brand_id}/roadmap")
async def roadmap(brand_id: str) -> dict:
    """The Roadmap page: goals + the latest approved plan + what's queued to
    publish (drafts awaiting work or review)."""
    await Brand.get(brand_id)
    plan = await Artifact.latest(brand_id, type="plan", statuses=[STATUS_APPROVED, STATUS_AUTO_APPROVED])
    draft_tasks = await Task.in_flight(brand_id, "content_draft")
    return {
        "goals": [{"metric": goal.metric, "target": float(goal.target), "deadline": goal.deadline.isoformat() if goal.deadline else None} for goal in await Goal.active(brand_id)],
        "plan": plan.content if plan else None,
        "publishing_next": [{"title": (task.payload or {}).get("entry", {}).get("title")} for task in draft_tasks],
    }


@router.get("/organizations/{organization_id}/pulse")
async def pulse(organization_id: str, limit: int = 50) -> dict:
    """The Pulse feed, newest first."""
    return {
        "items": [
            {
                "id": notification.id,
                "kind": notification.kind,
                "urgency": notification.urgency,
                "title": notification.title,
                "body": notification.body,
                "link_ref": notification.link_ref,
                "read_at": notification.read_at.isoformat() if notification.read_at else None,
                "created_at": notification.created_at.isoformat(),
            }
            for notification in await Notification.for_organization(organization_id, limit=limit)
        ]
    }


@router.get("/brands/{brand_id}/reports")
async def reports(brand_id: str) -> dict:
    """The report archive, newest first."""
    await Brand.get(brand_id)
    return {"reports": [{"id": report.id, "period": report.period.isoformat(), "published_at": report.published_at.isoformat() if report.published_at else None} for report in await Report.for_brand(brand_id)]}


@router.get("/reports/{report_id}")
async def report_detail(report_id: str) -> dict:
    report = await Report.get(report_id)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="report not found")
    return {"id": report.id, "brand_id": report.brand_id, "period": report.period.isoformat(), "narrative": report.narrative, "metrics": report.metrics, "published_at": report.published_at.isoformat() if report.published_at else None}


# TODO(attribution) GET /brands/{brand_id}/attribution — the "where the
#       revenue came from" page: four GROUP-BY sums over the leads table
#       (by source_channel, by AI engine from source_detail, by landing_path
#       with built-by-Evergreen provenance via the drafts ledger, by query).
#       ~25 lines; build together with the Shopify orders webhook
#       (api/webhooks.py's TODO(leads: orders)) — its first data source.


@router.get("/brands/{brand_id}/leads")
async def leads(brand_id: str, limit: int = 100) -> dict:
    """The leads/orders list (empty until a lead producer exists)."""
    await Brand.get(brand_id)
    return {
        "leads": [
            {"id": lead.id, "kind": lead.kind, "value_usd": float(lead.value_usd) if lead.value_usd is not None else None, "source_channel": lead.source_channel, "source_detail": lead.source_detail, "landing_path": lead.landing_path, "occurred_at": lead.occurred_at.isoformat()}
            for lead in await Lead.for_brand(brand_id, limit=limit)
        ]
    }
