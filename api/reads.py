"""The FE's read layer — one GET per mockup surface, deliberately simple:
plain dict responses (no schemas file), each shaped for its page so the FE
renders without stitching. Screens -> endpoints:

  Results (home)          GET /brands/{id}/overview
  Tracked questions       GET /brands/{id}/questions
  Work (+audit/activity/  GET /brands/{id}/worklog
    changes sub-pages)
  One site change         GET /change_sets/{id}   (the receipt: plain before/after)
  Your tasks              GET /brands/{id}/approvals   (+ GET /artifacts/{id} detail)
  Roadmap                 GET /brands/{id}/roadmap
  Pulse                   GET /organizations/{id}/pulse
  Reports archive/detail  GET /brands/{id}/reports · GET /reports/{id}
  Leads                   GET /brands/{id}/leads

Deliberately not served: per-city score scoping (dropped 2026-08-30 —
framework v1 scores brand-level) and a computed site-health score (counts
only, no invented formula). Auth rides the router-level guard
(utils/authz.py); GET /brands scopes by the caller's org itself."""

from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request, status

from objects.artifact import STATUS_APPROVED, STATUS_AUTO_APPROVED, STATUS_PENDING_REVIEW, STATUS_REJECTED, Artifact
from objects.brand import Brand
from objects.change_set import ChangeSet
from objects.competitor import SOURCE_AI_BOOTSTRAP, Competitor
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
from schemas.base_schemas import utc_iso
from utils import clock, receipts
from utils.authz import current_user
from worker.tasks.briefs import CLIENT_TASK_NAMES
from worker.tasks.site_audit import AUDIT_KINDS

router = APIRouter(prefix="/api/v1")

TREND_WEEKS = 13  # ~a quarter of weekly points for the hero chart
LEDGER_WINDOW_DAYS = 31  # "shipped this month"
DONE_FOR_YOU_DAYS = 30
DONE_FOR_YOU_LIMIT = 30
ISSUES_LIMIT = 50
CHANGE_LEDGER_LIMIT = 50  # rows the Changes ledger shows
CHANGE_SET_SCAN_LIMIT = 200  # rows read to derive issue status + undo links


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
                "approval_preset": brand.approval_preset,
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

    first_of_this_month = clock.today().replace(day=1)
    first_of_last_month = (first_of_this_month - timedelta(days=1)).replace(day=1)
    return {
        "brand": {"id": brand.id, "name": brand.name, "website_url": brand.website_url, "footprint": brand.footprint, "platform": brand.platform, "description": brand.description, "category": brand.category, "audience": brand.audience, "differentiators": brand.differentiators},
        "competitors": [competitor.name for competitor in await Competitor.get_competitors_by_brand_id(brand.id)],
        "brief": {"markdown": latest_brief.content.get("markdown"), "created_at": utc_iso(latest_brief.created_at)} if latest_brief else None,
        "visibility": visibility,
        "share_of_voice": share_of_voice,
        "metrics": {
            "this_month": await MetricPoint.totals_for_period(brand.id, first_of_this_month, clock.today() + timedelta(days=1)),
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
    what's running/queued, the shipped ledger, and applied change sets —
    plus the owner-language surfaces (utils/receipts.py): `issues` (every
    problem with a status dot), `done_for_you` (outcomes only, no plumbing),
    and per-change headlines / who approved / undo links."""
    await Brand.get(brand_id)
    month_ago = clock.now() - timedelta(days=LEDGER_WINDOW_DAYS)
    done_window_start = clock.now() - timedelta(days=DONE_FOR_YOU_DAYS)

    running_and_queued = await Task.for_brand(brand_id, statuses=[STATUS_RUNNING, STATUS_QUEUED])
    machine_tasks = [task for task in running_and_queued if task.name not in CLIENT_TASK_NAMES]
    done_tasks = [task for task in await Task.for_brand(brand_id, statuses=[STATUS_DONE]) if task.finished_at and task.finished_at > month_ago]

    open_findings = await Finding.open_for_brand(brand_id)
    resolved_findings = await Finding.resolved_since(brand_id, since=clock.today() - timedelta(days=LEDGER_WINDOW_DAYS))
    all_change_sets = await ChangeSet.for_brand(brand_id, limit=CHANGE_SET_SCAN_LIMIT)
    artifacts_by_id = {artifact.id: artifact for artifact in await Artifact.by_ids(change_set.artifact_id for change_set in all_change_sets)}
    proposals = await Artifact.for_brand(brand_id, type="change_proposal", statuses=[STATUS_PENDING_REVIEW, STATUS_APPROVED, STATUS_AUTO_APPROVED, STATUS_REJECTED])

    return {
        "site_health": {
            "open": [{"kind": finding.kind, "severity": finding.severity, "summary": finding.summary, "found_at": utc_iso(finding.created_at)} for finding in open_findings],
            "resolved_recently": [{"kind": finding.kind, "summary": finding.summary, "resolved_at": utc_iso(finding.resolved_at)} for finding in resolved_findings],
        },
        "issues": _issues(open_findings + resolved_findings, proposals, all_change_sets),
        "done_for_you": await _done_for_you(brand_id, all_change_sets, artifacts_by_id, done_window_start),
        "running": [{"id": task.id, "name": task.name, "started": utc_iso(task.created_at)} for task in machine_tasks if task.status == STATUS_RUNNING],
        "queued": [{"id": task.id, "name": task.name} for task in machine_tasks if task.status == STATUS_QUEUED],
        "shipped": [{"id": task.id, "name": task.name, "finished_at": utc_iso(task.finished_at)} for task in done_tasks],
        "change_sets": [_change_set_entry(change_set, artifacts_by_id.get(change_set.artifact_id), all_change_sets) for change_set in all_change_sets[:CHANGE_LEDGER_LIMIT]],
    }


def _change_set_entry(change_set: ChangeSet, artifact: Artifact | None, change_sets: list[ChangeSet]) -> dict:
    """One Changes-ledger row: the original keys + the owner-language ones."""
    original = next((candidate for candidate in change_sets if candidate.reverted_by_change_set_id == change_set.id), None)
    return {
        "id": change_set.id,
        "status": change_set.status,
        "operation_count": len(change_set.operations),
        "operations": [_ledger_operation(operation) for operation in change_set.operations],
        "applied_at": utc_iso(change_set.applied_at),
        "reverted_by": change_set.reverted_by_change_set_id,
        **receipts.ledger_entry(change_set, artifact, original),
    }


def _issues(findings: list[Finding], proposals: list[Artifact], change_sets: list[ChangeSet]) -> list[dict]:
    proposals_by_finding: dict[str, list[Artifact]] = {}
    for proposal in proposals:
        for finding_id in receipts.proposal_finding_ids(proposal.content):
            proposals_by_finding.setdefault(finding_id, []).append(proposal)
    change_sets_by_artifact: dict[str, list[ChangeSet]] = {}
    for change_set in change_sets:
        change_sets_by_artifact.setdefault(change_set.artifact_id, []).append(change_set)
    return receipts.order_issues([receipts.issue_entry(finding, proposals_by_finding.get(finding.id, []), change_sets_by_artifact) for finding in findings], limit=ISSUES_LIMIT)


async def _done_for_you(brand_id: str, change_sets: list[ChangeSet], artifacts_by_id: dict[str, Artifact], since) -> list[dict]:
    """Outcomes only (receipts.done_for_you): live/undone fixes, written work,
    audits and research with what they found, reports. Internal plumbing
    (applies, collection batches, syncs, ticks, client tasks) never appears.
    A sent pitch is "outreach_sent" ("Emailed <site>"), dated by its send."""
    items: list = receipts.change_outcomes(change_sets, artifacts_by_id, since)
    recent_artifacts = await Artifact.created_since(brand_id, since)
    items += [receipts.artifact_outcome(artifact) for artifact in recent_artifacts]
    # a pitch found before the window but EMAILED inside it is dated by its send
    recent_ids = {artifact.id for artifact in recent_artifacts}
    items += [receipts.artifact_outcome(pitch) for pitch in await Artifact.for_brand(brand_id, type="pitch", statuses=[STATUS_APPROVED]) if pitch.id not in recent_ids]
    items += [receipts.report_outcome(report) for report in await Report.for_brand(brand_id) if report.published_at and report.published_at >= since]

    audits = await Task.done_since(brand_id, ["site_audit"])
    first_audit_id = min(audits, key=lambda task: task.created_at).id if audits else None
    recent_audits = [task for task in audits if task.finished_at and task.finished_at >= since]
    if recent_audits:
        found_moments = [finding.created_at for finding in await Finding.created_since(brand_id, min(task.created_at for task in recent_audits)) if finding.kind in AUDIT_KINDS]
        items += [receipts.audit_outcome(task, receipts.count_in_window(found_moments, task.created_at, task.finished_at), is_first=task.id == first_audit_id) for task in recent_audits]

    research_runs = await Task.done_since(brand_id, ["research_prompts", "research_competitors"], since=since)
    if research_runs:
        prompt_moments = [prompt.created_at for prompt in await Prompt.by_brand(brand_id) if prompt.source == "generated"]
        competitor_moments = [competitor.created_at for competitor in await Competitor.get_competitors_by_brand_id(brand_id) if competitor.source == SOURCE_AI_BOOTSTRAP]
        for task in research_runs:
            moments = prompt_moments if task.name == "research_prompts" else competitor_moments
            items.append(receipts.research_outcome(task, receipts.count_in_window(moments, task.created_at, task.finished_at)))
    return receipts.done_for_you(items, since, limit=DONE_FOR_YOU_LIMIT)


@router.get("/change_sets/{change_set_id}")
async def change_set_detail(change_set_id: str) -> dict:
    """One site change, as a receipt: the ledger entry + `changes`, a plain
    before/after per operation (never raw HTML). Auth rides the router-level
    guard (change_set_id -> its brand's org)."""
    change_set = await ChangeSet.get(change_set_id)
    original = await ChangeSet.reverted_from(change_set.id)
    artifacts = await Artifact.by_ids([change_set.artifact_id])
    artifact = artifacts[0] if artifacts else None
    return {
        "id": change_set.id,
        "brand_id": change_set.brand_id,
        "status": change_set.status,
        "operation_count": len(change_set.operations),
        "operations": [_ledger_operation(operation) for operation in change_set.operations],
        "applied_at": utc_iso(change_set.applied_at),
        "reverted_by": change_set.reverted_by_change_set_id,
        **receipts.ledger_entry(change_set, artifact, original),
        "changes": receipts.change_details(change_set, original),
    }


def _ledger_operation(operation: dict) -> dict:
    """What the Changes ledger says about one op (kind, field, page, a new
    page's title) — never the values themselves, which can be whole pages."""
    value = operation.get("value")
    return {"op": operation.get("op"), "field": operation.get("field"), "target": operation.get("target"), "title": value.get("title") if isinstance(value, dict) else None}


@router.get("/brands/{brand_id}/approvals")
async def approvals(brand_id: str) -> dict:
    """The Your-tasks page: the approvals lane (pending artifacts, oldest
    first) and the we-need-from-you lane (open client-contribution tasks)."""
    await Brand.get(brand_id)
    client_tasks = [task for task in await Task.for_brand(brand_id, statuses=[STATUS_QUEUED]) if task.name in CLIENT_TASK_NAMES]
    return {
        "pending": [
            {"id": artifact.id, "type": artifact.type, "created_at": utc_iso(artifact.created_at), "content": artifact.content, "evidence": artifact.evidence, **({"workstreams": receipts.plan_workstreams(artifact.content)} if artifact.type == "plan" else {})}
            for artifact in await Artifact.pending_for_brand(brand_id)
        ],
        "waiting_on_you": [{"id": task.id, "name": task.name, "days_waiting": (clock.now() - task.created_at).days if task.created_at else 0} for task in client_tasks],
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
        "created_at": utc_iso(artifact.created_at),
        "reviewed_by": artifact.reviewed_by,
        "reviewed_at": utc_iso(artifact.reviewed_at),
    }


@router.get("/brands/{brand_id}/roadmap")
async def roadmap(brand_id: str) -> dict:
    """The Roadmap page: goals + the latest approved plan + what's queued to
    publish (drafts awaiting work or review)."""
    await Brand.get(brand_id)
    plan = await Artifact.latest(brand_id, type="plan", statuses=[STATUS_APPROVED, STATUS_AUTO_APPROVED])
    draft_tasks = await Task.in_flight(brand_id, "content_draft")
    # a workstream "needs you" while its client-data task (#25) still waits
    study_waiting = {(task.payload or {}).get("workstream") for task in await Task.in_flight(brand_id, "collect_study_data")}
    return {
        "goals": [{"metric": goal.metric, "target": float(goal.target), "deadline": goal.deadline.isoformat() if goal.deadline else None} for goal in await Goal.active(brand_id)],
        "plan": plan.content if plan else None,
        "workstreams": receipts.plan_workstreams(plan.content, needs_you_names={name for name in study_waiting if name}) if plan else [],
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
                "read_at": utc_iso(notification.read_at),
                "created_at": utc_iso(notification.created_at),
            }
            for notification in await Notification.for_organization(organization_id, limit=limit)
        ]
    }


@router.post("/organizations/{organization_id}/pulse/read")
async def pulse_read(organization_id: str) -> dict:
    """Opening Pulse reads it, so the nav badge only counts what's new since."""
    return {"marked": await Notification.mark_all_read(organization_id)}


@router.get("/brands/{brand_id}/reports")
async def reports(brand_id: str) -> dict:
    """The report archive, newest first."""
    await Brand.get(brand_id)
    return {"reports": [{"id": report.id, "period": report.period.isoformat(), "published_at": utc_iso(report.published_at)} for report in await Report.for_brand(brand_id)]}


@router.get("/reports/{report_id}")
async def report_detail(report_id: str) -> dict:
    report = await Report.get(report_id)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="report not found")
    return {"id": report.id, "brand_id": report.brand_id, "period": report.period.isoformat(), "narrative": report.narrative, "metrics": report.metrics, "published_at": utc_iso(report.published_at)}


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
            {"id": lead.id, "kind": lead.kind, "value_usd": float(lead.value_usd) if lead.value_usd is not None else None, "source_channel": lead.source_channel, "source_detail": lead.source_detail, "landing_path": lead.landing_path, "occurred_at": utc_iso(lead.occurred_at)}
            for lead in await Lead.for_brand(brand_id, limit=limit)
        ]
    }
