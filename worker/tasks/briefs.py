"""The Account Manager's two cadences: the Monday Brief and the daily chase.

Both are AM-agent calls over assembler-supplied facts — the numbers come
from the database, the agent only writes prose over them. Output lands as
Pulse rows (Notification) + a brief artifact; email/SMS delivery rides the
notifier adapter when it lands (the Notification row IS the in-app Pulse)."""

import json
from datetime import datetime, time, timedelta

from sqlalchemy import select

from adapters.agents.claude_agent import ClaudeAgent
from objects.artifact import STATUS_AUTO_APPROVED, Artifact
from objects.brand import Brand
from objects.change_set import STATUS_REVERTED, ChangeSet
from objects.connection import Connection
from objects.finding import Finding
from objects.notification import KIND_UPDATE, Notification
from objects.task import STATUS_DONE, STATUS_QUEUED, Task
from objects.visibility_run import VisibilityRun
from utils import clock
from utils.agent_prompts import ROLE_ACCOUNT_MANAGER
from utils.database import ReplicaDatabase
from utils.logger import logger
from utils.task_queue import RETRY_WITH_BACKOFF, task_lifecycle
from worker.app import app


@app.task(name="weekly_brief", queue="am", retry=RETRY_WITH_BACKOFF)
async def weekly_brief(task_id: str, brand_id: str) -> None:
    """Pulse — the Monday Brief: what moved, what shipped, what's waiting on
    the client. Facts are assembled from the DB; the AM agent writes prose
    over them and never invents a number. Emits a `brief` artifact
    (auto-approved — informational, no side effects) + a Pulse notification."""
    async with task_lifecycle(task_id):
        brand = await Brand.get(brand_id)
        facts = await _assemble_brief_facts(brand)

        brief_markdown = await ClaudeAgent().run_role(
            ROLE_ACCOUNT_MANAGER,
            prompt=_brief_prompt(brand, facts),
            tools=[],
            handlers={},
            operation="weekly_brief",
            organization_id=brand.organization_id,
            brand_id=brand.id,
            task_id=task_id,
        )

        brief = await Artifact.create(task_id=task_id, brand_id=brand.id, type="brief", content={"markdown": brief_markdown}, evidence=facts, status=STATUS_AUTO_APPROVED)
        await Notification.create(organization_id=brand.organization_id, kind=KIND_UPDATE, title="Your weekly brief is ready", link_ref=f"artifacts/{brief.id}")


@app.task(name="client_chase", queue="am", retry=RETRY_WITH_BACKOFF)
async def client_chase(task_id: str, brand_id: str) -> None:
    """#1 (the chase half) — nudge the client about their overdue
    contributions with escalating gentleness. No-ops when nothing is overdue
    or the org was nudged within the cooldown, so the daily tick is cheap."""
    async with task_lifecycle(task_id):
        brand = await Brand.get(brand_id)

        overdue_tasks = await overdue_client_tasks(brand.id)
        if not overdue_tasks:
            logger.info(f"[client_chase] brand {brand.id}: nothing overdue")
            return
        if await _chased_recently(brand.organization_id):
            logger.info(f"[client_chase] brand {brand.id}: within cooldown, skipping")
            return

        days_waiting = max(_days_since(overdue_task.created_at) for overdue_task in overdue_tasks)
        nudge_text = await ClaudeAgent().run_role(
            ROLE_ACCOUNT_MANAGER,
            prompt=_chase_prompt(brand, overdue_tasks, days_waiting),
            tools=[],
            handlers={},
            operation="client_chase",
            organization_id=brand.organization_id,
            brand_id=brand.id,
            task_id=task_id,
        )
        await Notification.create(organization_id=brand.organization_id, kind=KIND_UPDATE, title=CHASE_TITLE, body=nudge_text, link_ref="tasks")


# Client-contribution tasks (rows with no queue job): the first four are
# created by onboarding's launch; collect_study_data by plan approval (#25).
# The chase only ever nudges THESE names — internal work is never chased.
CLIENT_TASK_NAMES = {"grant_access", "upload_photos", "answer_sme_interview", "verify_conversion_tracking", "collect_study_data"}
CHASE_AFTER_DAYS = 3  # how long a client task may sit before the first nudge
CHASE_COOLDOWN_DAYS = 3  # never nudge the same org more often than this
CHASE_TITLE = "A gentle nudge from your Evergreen team"

# How the Brief AND the monthly report read (reports.py reuses it): the owner
# reads the first few words on his phone and never a paragraph
# (customer-persona.md) — one number-bearing line, then a few short bullets.
OWNER_UPDATE_FORMAT = (
    "FORMAT (the owner reads only the first few words, on his phone — follow it exactly):\n"
    "- Line 1: one plain sentence under 12 words that contains a number: what we did or what changed.\n"
    '- Then at most 5 bullets ("- "), each under 12 words, in plain everyday words.\n'
    "- No jargon: never write schema, SEO, visibility score, share of voice, SoV, indexing, structured data,\n"
    "  detection rate or findings. Say what it means for customers finding and calling them.\n"
    '- A final bullet starting "Needs you:" ONLY when the facts show something waiting on the owner;\n'
    "  otherwise leave it out.\n"
    "- No headings, no tables, no paragraphs, nothing before line 1 or after the bullets.\n"
)


##############################
# BRIEF FACTS
##############################


async def _assemble_brief_facts(brand: Brand) -> dict:
    """Everything the Brief may reference — assembled here, never by the agent."""
    week_ago = clock.now() - timedelta(days=7)

    latest_runs = await VisibilityRun.latest(brand.id, limit=2)
    visibility: dict = {"status": "no completed visibility run yet"}
    if latest_runs:
        current_run = latest_runs[0]
        visibility = {
            "week_of": current_run.week_of.isoformat(),
            "overall_score": current_run.overall_score,
            "detection_rate": current_run.detection_rate,
            "share_of_voice": current_run.share_of_voice,
        }
        if len(latest_runs) == 2:
            previous_run = latest_runs[1]
            visibility["change_vs_last_week"] = round((current_run.overall_score or 0) - (previous_run.overall_score or 0), 4)

    open_findings = await Finding.open_for_brand(brand.id)
    new_findings = [finding for finding in open_findings if finding.created_at and finding.created_at > week_ago]
    task_counts = await completed_task_counts(brand.id, since=week_ago)

    return {
        "visibility": visibility,
        "site_health": {
            "open_findings": len(open_findings),
            "new_this_week": [{"kind": finding.kind, "summary": finding.summary} for finding in new_findings],
        },
        "work_this_week": await work_facts(brand.id, task_counts, await ChangeSet.for_brand(brand.id), since=week_ago),
        "waiting_on_your_approval_now": pending_approval_counts(await Artifact.pending_for_brand(brand.id)),
        "waiting_on_client": [{"task": client_task.name, "days_waiting": _days_since(client_task.created_at)} for client_task in await overdue_client_tasks(brand.id)],
    }


async def completed_task_counts(brand_id: str, since: datetime, until: datetime | None = None) -> dict[str, int]:
    """Done tasks by name, finished in [since, until); open-ended when until is None."""
    statement = select(Task.name).where(Task.brand_id == brand_id).where(Task.status == STATUS_DONE).where(Task.finished_at >= since)  # type: ignore
    if until is not None:
        statement = statement.where(Task.finished_at < until)  # type: ignore
    rows = await ReplicaDatabase.session.execute(statement)  # type: ignore
    counts: dict[str, int] = {}
    for (task_name,) in rows:
        counts[task_name] = counts.get(task_name, 0) + 1
    return counts


##############################
# WORK FACTS — what actually happened, never raw run counts (BUG-050)
##############################

WORK_HOW_TO_READ = (
    "Each entry states what actually happened. Nothing drafted was sent, posted or published. Activity counts runs and drafts made in the period, not what is waiting now:"
    " a run can produce nothing, and a draft may since have been approved or rejected, so never state how many items await approval from these counts."
    " Only site_changes.changes_live_on_site_now changed the website; a change that was undone is net zero, and an undo is not a new change."
)
# a DONE task of each name -> what it actually produced (a run can also produce nothing)
WORK_OUTCOMES = {
    "site_audit": "site health checks run (a check only; nothing on the site changed)",
    "technical_fix": "site-fix runs (a run reviews open findings and may propose zero or more fixes; a run count, not a proposal count; nothing on the site changes until a proposal is approved and applied)",
    "technical_judgment": "written site-fix recommendation runs (nothing applied)",
    "content_calendar": "content calendars drafted (nothing published)",
    "content_draft": "articles drafted (not published)",
    "content_refresh": "page rewrites drafted (not published)",
    "review_asks": "review-request template upkeep runs (templates only: no review request has been sent to any customer)",
    "review_response": "review-reply drafting runs (not posted)",
    "outreach_scout": "outreach searches run (any pitch found is a draft; a pitch is emailed only after the owner approves it)",
    "send_pitch": "outreach emails the owner approved for sending (one may have been taken back before it went out; not a count of emails delivered)",
    "research_prompts": "search-question research runs",
    "research_competitors": "competitor research runs",
    "metrics_ingestion": "traffic data syncs from connected accounts",
    "alert_detection": "automatic checks for drops and problems",
    "visibility_run": "weekly AI-visibility measurements",
    "submit_prompt_execution_batch": "AI-answer collection batches submitted",
    "diagnosis": "drop diagnoses written",
    "plan_update": "roadmaps drafted",
    "weekly_brief": "weekly briefs written",
    "monthly_report": "monthly reports written",
    "client_chase": "checks for items waiting on the client",
}
# reported from the change_sets rows / GBP connection instead of their run counts
CHANGE_TASK_NAMES = {"change_set_apply"}
GBP_TASK_NAMES = {"gbp_management", "gbp_apply"}

CHANGE_LIVE = "change, still live on the site"
CHANGE_UNDONE = "change, since undone (net zero)"
CHANGE_UNDO = "undo of an earlier change (not a new change)"


async def work_facts(brand_id: str, task_counts: dict[str, int], change_sets: list[ChangeSet], since: datetime, until: datetime | None = None) -> dict:
    """describe_work over the DB's answer to "is GBP connected" (asked only when there is GBP work to describe)."""
    gbp_connected = bool(GBP_TASK_NAMES & task_counts.keys()) and any(connection.provider == "gbp" for connection in await Connection.connected_for_brand(brand_id))
    return describe_work(task_counts, change_sets, since, until, gbp_connected)


def describe_work(task_counts: dict[str, int], change_sets: list, since: datetime, until: datetime | None, gbp_connected: bool) -> dict:
    """The period's work as outcomes the Account Manager can't misread: drafts
    are drafts, site changes are counted from change_sets net of undos, and
    GBP items park as "waiting to be posted by hand" while GBP isn't connected. Pure."""
    activity = []
    for task_name, count in sorted(task_counts.items()):
        if task_name in CHANGE_TASK_NAMES or task_name in GBP_TASK_NAMES:
            continue
        if task_name in CLIENT_TASK_NAMES:
            label = f"completed by the client: {task_name.replace('_', ' ')}"
        else:
            label = WORK_OUTCOMES.get(task_name, f"{task_name.replace('_', ' ')} runs")
        activity.append({"what_happened": label, "times": count})

    revert_set_ids = {change_set.reverted_by_change_set_id for change_set in change_sets if change_set.reverted_by_change_set_id}
    outcomes = [change_set_outcome(change_set, revert_set_ids) for change_set in change_sets if change_set.applied_at and change_set.applied_at >= since and (until is None or change_set.applied_at < until)]
    facts: dict = {
        "how_to_read": WORK_HOW_TO_READ,
        "activity": activity,
        "site_changes": {"changes_live_on_site_now": outcomes.count(CHANGE_LIVE), "changes_applied_then_undone": outcomes.count(CHANGE_UNDONE), "undos_of_earlier_changes": outcomes.count(CHANGE_UNDO)},
    }
    drafted, approved = task_counts.get("gbp_management", 0), task_counts.get("gbp_apply", 0)
    if drafted or approved:
        if gbp_connected:
            facts["google_business_profile"] = {"connected": True, "post_batches_drafted": drafted, "approved_items_posted_to_google": approved}
        else:
            facts["google_business_profile"] = {
                "connected": False,
                "status": "not connected: nothing is posted to Google automatically and the profile is not being managed live",
                "post_batches_drafted": drafted,
                "approved_items_waiting_to_be_posted_by_hand": approved,
            }
    return facts


# artifact type -> what the owner sees waiting in Approvals
PENDING_LABELS = {
    "change_proposal": "site-fix proposals",
    "draft": "article or page drafts",
    "calendar": "content calendars",
    "plan": "roadmaps",
    "reply": "review replies",
    "pitch": "outreach pitches",
    "gbp_touches": "Google Business Profile post batches",
    "flag_request": "requests for your input",
    "review_ask_templates": "review-request templates",
    "review_ask": "review-request texts",
    "diagnosis": "drop diagnoses",
}


def pending_approval_counts(pending_artifacts: list) -> dict:
    """What is waiting on the owner NOW, per type, counted from pending_review
    artifacts (never from runs, BUG-054). Pure."""
    counts: dict[str, int] = {}
    for artifact in pending_artifacts:
        label = PENDING_LABELS.get(artifact.type, artifact.type.replace("_", " "))
        counts[label] = counts.get(label, 0) + 1
    return {"total": sum(counts.values()), "by_type": counts}


def change_set_outcome(change_set, revert_set_ids: set[str]) -> str:
    """What one applied change set did to the site now: an undo, a change
    later undone, or a change still live. Pure."""
    if change_set.id in revert_set_ids:
        return CHANGE_UNDO
    if change_set.status == STATUS_REVERTED or change_set.reverted_by_change_set_id:
        return CHANGE_UNDONE
    return CHANGE_LIVE


def _brief_prompt(brand: Brand, facts: dict) -> str:
    return (
        "Write this week's Brief for the client below, in markdown.\n"
        "Lead with what changed. Use ONLY the facts provided — if something has no data yet, say so plainly\n"
        "in a short bullet (or leave it out) rather than inventing anything.\n"
        "The work facts say what actually happened: a draft was not sent, posted or published, an undone\n"
        "change is not a change, and nothing is live or managed on a service the facts say is not connected.\n"
        "Only waiting_on_your_approval_now says what awaits their approval; quote its counts exactly.\n"
        "Something waits on the owner when waiting_on_your_approval_now or waiting_on_client is not empty.\n\n"
        f"{OWNER_UPDATE_FORMAT}\n"
        f"Brand: {brand.name} ({brand.website_url})\n\n"
        f"Facts:\n{json.dumps(facts, indent=2, default=str)}"
    )


##############################
# CHASE
##############################


async def overdue_client_tasks(brand_id: str) -> list[Task]:
    """Queued client-contribution tasks older than the grace period."""
    overdue_cutoff = calendar_days_cutoff(CHASE_AFTER_DAYS)
    rows = await ReplicaDatabase.session.execute(select(Task).where(Task.brand_id == brand_id).where(Task.status == STATUS_QUEUED).where(Task.name.in_(CLIENT_TASK_NAMES)).where(Task.created_at < overdue_cutoff))  # type: ignore
    return list(rows.scalars().all())


def calendar_days_cutoff(days: int) -> datetime:
    """Midnight starting the (days-1)th day back: anything stamped on day N is before the
    cutoff from day N+days on. Calendar days, not exact hours (DEC-021 A), so time of day
    — a nudge stamped after a slow AM call, a task created mid-afternoon — never slips
    the daily 09:00 tick to day N+days+1 (BUG-047)."""
    return datetime.combine(clock.today() - timedelta(days=days - 1), time.min)


async def _chased_recently(organization_id: str) -> bool:
    cooldown_cutoff = calendar_days_cutoff(CHASE_COOLDOWN_DAYS)
    rows = await ReplicaDatabase.session.execute(select(Notification.id).where(Notification.organization_id == organization_id).where(Notification.title == CHASE_TITLE).where(Notification.created_at >= cooldown_cutoff).limit(1))  # type: ignore
    return rows.first() is not None


def _days_since(moment: datetime | None) -> int:
    if moment is None:
        return 0
    return max((clock.now() - moment).days, 0)


def _chase_prompt(brand: Brand, overdue_tasks: list[Task], days_waiting: int) -> str:
    task_lines = "\n".join(f"- {overdue_task.name.replace('_', ' ')} (waiting {_days_since(overdue_task.created_at)} days)" for overdue_task in overdue_tasks)
    return (
        "Write a short nudge to the client about the items below that are waiting on them.\n"
        f"The oldest has waited {days_waiting} days — calibrate your tone to that: friendly reminder in the\n"
        "first week, plainer and more direct as weeks pass. Never passive-aggressive. Always restate,\n"
        "concretely, what completing each item unblocks. Under 120 words, no subject line.\n\n"
        f"Brand: {brand.name}\n\nWaiting on them:\n{task_lines}"
    )
