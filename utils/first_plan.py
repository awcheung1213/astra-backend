"""The first roadmap waits for its inputs.

Launch used to queue plan_update on the spot, so the Strategist planned the
first quarter before the first audit or the interview's research had landed
and said so in its own text ("no crawl has been run") — e2e 2026-09-15, gap 3.
Now every producer of a first-plan input (site_audit, research_prompts,
research_competitors) calls queue_first_plan_when_ready as it finishes, and
launch calls it too; whoever finishes LAST queues the plan. Quarterly
refreshes stay the scheduler's (a brand with a plan never gets one here).

An input that fails FOR GOOD counts as settled (BUG-057): task_lifecycle runs
FINAL_FAILURE_HOOKS on the last attempt, so the plan still goes ahead with
whatever landed (the Strategist plans without the missing input) and the
owner hears which step is missing."""

from objects.artifact import Artifact
from objects.brand import Brand
from objects.notification import KIND_UPDATE, URGENCY_NORMAL, Notification
from objects.task import Task
from utils.logger import logger
from utils.task_queue import create_task

FIRST_PLAN_INPUTS = ("site_audit", "research_prompts", "research_competitors")


async def queue_first_plan_when_ready(brand_id: str, completing_task_id: str | None = None) -> bool:
    """Queue plan_update once the brand is launched, has no plan yet, and no
    first-plan input (other than the task now completing) is still in
    flight. Returns True when it queued."""
    brand = await Brand.get(brand_id)
    if brand.onboarded_at is None:
        return False  # pre-launch research: launch decides
    if await Artifact.latest(brand_id, type="plan") is not None:
        return False
    if await Task.in_flight(brand_id, "plan_update"):
        return False
    for input_name in FIRST_PLAN_INPUTS:
        if [task for task in await Task.in_flight(brand_id, input_name) if task.id != completing_task_id]:
            return False
    await create_task(brand_id=brand_id, name="plan_update", queue="planning")
    logger.info(f"[planning] brand {brand_id}: first-plan inputs landed — plan_update queued")
    return True


# owner copy per input: (title, what happens next) — each "next" names a real re-run path
FIRST_PLAN_INPUT_FAILURE_COPY = {
    "site_audit": ("We couldn't finish checking your website", "We'll check it again on the next weekly site check."),
    "research_prompts": ("We couldn't finish researching the questions your customers ask AI", "We'll run that research again when you approve your roadmap."),
    "research_competitors": ("We couldn't finish researching your competitors", "We'll keep adding competitors as we spot them in AI answers, and you can add your own anytime."),
}


async def first_plan_input_failed(brand_id: str, task_id: str, task_name: str) -> bool:
    """A first-plan input failed on its last attempt: re-run the readiness
    check (the failed row is no longer in flight, so it counts as settled) and
    tell the owner. Pre-launch or already-planned brands are left alone —
    launch decides / the roadmap isn't waiting on this. Returns True when it
    queued the plan."""
    brand = await Brand.get(brand_id)
    if brand.onboarded_at is None or await Artifact.latest(brand_id, type="plan") is not None:
        return False
    queued = await queue_first_plan_when_ready(brand_id, task_id)
    title, next_step = FIRST_PLAN_INPUT_FAILURE_COPY[task_name]
    await Notification.create(organization_id=brand.organization_id, kind=KIND_UPDATE, urgency=URGENCY_NORMAL, title=title, body=f"Your first roadmap is being built without it. {next_step}")
    return queued


# task name -> hook(brand_id, task_id, task_name), run by task_lifecycle when that task fails on its last attempt
FINAL_FAILURE_HOOKS = {name: first_plan_input_failed for name in FIRST_PLAN_INPUTS}
