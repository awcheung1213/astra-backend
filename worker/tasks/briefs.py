"""The Account Manager's two cadences: the Monday Brief and the daily chase.

Both are AM-agent calls over assembler-supplied facts — the numbers come
from the database, the agent only writes prose over them. Output lands as
Pulse rows (Notification) + a brief artifact; email/SMS delivery rides the
notifier adapter when it lands (the Notification row IS the in-app Pulse)."""

import json
from datetime import datetime, timedelta

from sqlalchemy import select

from adapters.agents.claude_agent import ClaudeAgent
from objects.artifact import STATUS_AUTO_APPROVED, Artifact
from objects.brand import Brand
from objects.finding import Finding
from objects.notification import KIND_UPDATE, Notification
from objects.task import STATUS_DONE, STATUS_QUEUED, Task
from objects.visibility_run import VisibilityRun
from utils.agent_prompts import ROLE_ACCOUNT_MANAGER
from utils.database import ReplicaDatabase
from utils.logger import logger
from utils.task_queue import task_lifecycle
from worker.app import app


@app.task(name="weekly_brief", queue="am", retry=2)
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


@app.task(name="client_chase", queue="am", retry=2)
async def client_chase(task_id: str, brand_id: str) -> None:
    """#1 (the chase half) — nudge the client about their overdue
    contributions with escalating gentleness. No-ops when nothing is overdue
    or the org was nudged within the cooldown, so the daily tick is cheap."""
    async with task_lifecycle(task_id):
        brand = await Brand.get(brand_id)

        overdue_tasks = await _overdue_client_tasks(brand.id)
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


# Client-contribution tasks (rows with no queue job, created by onboarding).
# The chase only ever nudges THESE names — internal work is never chased.
CLIENT_TASK_NAMES = {"grant_access", "upload_photos", "answer_sme_interview", "verify_conversion_tracking"}
CHASE_AFTER_DAYS = 3  # how long a client task may sit before the first nudge
CHASE_COOLDOWN_DAYS = 3  # never nudge the same org more often than this
CHASE_TITLE = "A gentle nudge from your Astra team"

BRIEF_MAX_WORDS = 250


##############################
# BRIEF FACTS
##############################


async def _assemble_brief_facts(brand: Brand) -> dict:
    """Everything the Brief may reference — assembled here, never by the agent."""
    week_ago = datetime.now() - timedelta(days=7)

    latest_runs = await VisibilityRun.latest(brand.id, limit=2)
    visibility: dict = {"status": "no completed visibility run yet"}
    if latest_runs:
        current_run = latest_runs[0]
        visibility = {
            "week_of": current_run.week_of,
            "overall_score": current_run.overall_score,
            "detection_rate": current_run.detection_rate,
            "share_of_voice": current_run.share_of_voice,
        }
        if len(latest_runs) == 2:
            previous_run = latest_runs[1]
            visibility["change_vs_last_week"] = round((current_run.overall_score or 0) - (previous_run.overall_score or 0), 4)

    open_findings = await Finding.open_for_brand(brand.id)
    new_findings = [finding for finding in open_findings if finding.created_at and finding.created_at > week_ago]

    return {
        "visibility": visibility,
        "site_health": {
            "open_findings": len(open_findings),
            "new_this_week": [{"kind": finding.kind, "summary": finding.summary} for finding in new_findings],
        },
        "work_shipped_this_week": await _completed_task_counts(brand.id, since=week_ago),
        "waiting_on_client": [{"task": client_task.name, "days_waiting": _days_since(client_task.created_at)} for client_task in await _overdue_client_tasks(brand.id)],
    }


async def _completed_task_counts(brand_id: str, since: datetime) -> dict[str, int]:
    rows = await ReplicaDatabase.session.execute(select(Task.name).where(Task.brand_id == brand_id).where(Task.status == STATUS_DONE).where(Task.finished_at > since))  # type: ignore
    counts: dict[str, int] = {}
    for (task_name,) in rows:
        counts[task_name] = counts.get(task_name, 0) + 1
    return counts


def _brief_prompt(brand: Brand, facts: dict) -> str:
    return (
        f"Write this week's Brief for the client below, in markdown, under {BRIEF_MAX_WORDS} words.\n"
        "Lead with what changed. Use ONLY the facts provided — if a section has no data yet, say so in one\n"
        "plain sentence rather than inventing anything. Close with what (if anything) you need from them.\n\n"
        f"Brand: {brand.name} ({brand.website_url})\n\n"
        f"Facts:\n{json.dumps(facts, indent=2, default=str)}"
    )


##############################
# CHASE
##############################


async def _overdue_client_tasks(brand_id: str) -> list[Task]:
    """Queued client-contribution tasks older than the grace period."""
    overdue_cutoff = datetime.now() - timedelta(days=CHASE_AFTER_DAYS)
    rows = await ReplicaDatabase.session.execute(select(Task).where(Task.brand_id == brand_id).where(Task.status == STATUS_QUEUED).where(Task.name.in_(CLIENT_TASK_NAMES)).where(Task.created_at < overdue_cutoff))  # type: ignore
    return list(rows.scalars().all())


async def _chased_recently(organization_id: str) -> bool:
    cooldown_cutoff = datetime.now() - timedelta(days=CHASE_COOLDOWN_DAYS)
    rows = await ReplicaDatabase.session.execute(select(Notification.id).where(Notification.organization_id == organization_id).where(Notification.title == CHASE_TITLE).where(Notification.created_at > cooldown_cutoff).limit(1))  # type: ignore
    return rows.first() is not None


def _days_since(moment: datetime | None) -> int:
    if moment is None:
        return 0
    return max((datetime.now() - moment).days, 0)


def _chase_prompt(brand: Brand, overdue_tasks: list[Task], days_waiting: int) -> str:
    task_lines = "\n".join(f"- {overdue_task.name.replace('_', ' ')} (waiting {_days_since(overdue_task.created_at)} days)" for overdue_task in overdue_tasks)
    return (
        "Write a short nudge to the client about the items below that are waiting on them.\n"
        f"The oldest has waited {days_waiting} days — calibrate your tone to that: friendly reminder in the\n"
        "first week, plainer and more direct as weeks pass. Never passive-aggressive. Always restate,\n"
        "concretely, what completing each item unblocks. Under 120 words, no subject line.\n\n"
        f"Brand: {brand.name}\n\nWaiting on them:\n{task_lines}"
    )
