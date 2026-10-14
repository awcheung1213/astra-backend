"""The approval gate's shared core: the always-approve floor, the status an
artifact is born with, and the fan-out an approval sets in motion.

Two callers: the review endpoint (api/approvals.py — a human clicked) and the
producers themselves (worker tasks), which consult initial_artifact_status at
birth and, for artifacts born auto_approved under the brand's preset, run the
SAME fan-out immediately. That birth-time dispatch is what makes Balanced and
Hands-off actually ship work instead of parking it in a queue nobody reads.

The floor (architecture rule 7) is CODE, never expressible as an
approval_settings row: work that leaves the building under the brand's name —
a public review reply, GBP posts going live, an outreach pitch emailed to a
stranger from the owner's own mailbox — always waits for a human, no matter
the preset. Site changes (change_proposal,
draft, calendar) are deliberately NOT floor: every operation the change-set
engine applies carries a rollback record, and auto-shipping that reversible
tier is exactly what the presets exist for."""

from objects.approval_setting import MODE_AUTO, ApprovalSetting
from objects.artifact import STATUS_AUTO_APPROVED, STATUS_PENDING_REVIEW, Artifact
from objects.change_set import ChangeSet
from objects.site import Site
from objects.task import Task
from utils.database import Database
from utils.logger import logger
from utils.pitch_mail import SEND_DELAY_SECONDS, SEND_SCHEDULED
from utils.task_queue import create_task
from worker.tasks.changes import APPLY_ENABLED_PLATFORMS, operations_for_artifact

# rule 7: the floor. Everything here is outward + irreversible — once a reply
# is posted, a pitch is emailed, or a GBP post is live, no rollback record can
# unsay it — so a human always clicks (a pitch's 30-second unsend window is
# a courtesy, not a rollback). (flag_request never dispatches and
# informational types never sit at pending review, so neither needs a row.)
ALWAYS_APPROVE_ARTIFACT_TYPES = {"reply", "gbp_touches", "pitch"}

# types whose approval is applied THROUGH the brand's connected site (the
# change-set engine). Approving one with no site connected strands it:
# recorded approved, nothing to apply, no way back to the queue — so both the
# review endpoint and the birth path refuse until a site is connected.
SITE_APPLIED_ARTIFACT_TYPES = {"change_proposal", "draft"}


def requires_human_approval(artifact_type: str) -> bool:
    """Consulted by every auto-approve path (approval_settings MODE_AUTO)
    BEFORE skipping the gate — floor types always return True."""
    return artifact_type in ALWAYS_APPROVE_ARTIFACT_TYPES


async def initial_artifact_status(brand_id: str, task_name: str, artifact_type: str) -> str:
    """The status an artifact is born with: floor first (a human always
    clicks, whatever the rows say), then the brand's approval_settings row for
    the PRODUCING task. Default mode is approve — no row means reviewed."""
    if requires_human_approval(artifact_type):
        return STATUS_PENDING_REVIEW
    if await needs_site_connection(brand_id, artifact_type):
        return STATUS_PENDING_REVIEW  # auto-approving now would strand it; it waits in the queue for the connect
    mode = await ApprovalSetting.mode_for(brand_id, task_name)
    return STATUS_AUTO_APPROVED if mode == MODE_AUTO else STATUS_PENDING_REVIEW


async def needs_site_connection(brand_id: str, artifact_type: str) -> bool:
    """True when approving this type would need the site engine and the brand
    has no connected site — the gate the review endpoint 409s on."""
    return artifact_type in SITE_APPLIED_ARTIFACT_TYPES and await Site.by_brand(brand_id) is None


async def fan_out_approval(artifact: Artifact, approved_rows: list[int] | None = None) -> list[str]:
    """What an approval sets in motion, by artifact type. Types with no entry
    need nothing (informational types never sit at pending review)."""
    if artifact.type == "pitch":
        # approving a pitch SENDS it: the review endpoint validated the
        # recipient + route and stamped content["send"] scheduled; the email
        # goes out after the unsend window (utils/pitch_mail.py)
        send = artifact.content.get("send") or {}
        if send.get("status") != SEND_SCHEDULED:
            logger.info(f"[approvals] pitch {artifact.id} approved with no scheduled send — nothing to dispatch")
            return []
        await create_task(brand_id=artifact.brand_id, name="send_pitch", queue="outreach", payload={"artifact_id": artifact.id, "send_id": send.get("id")}, delay_seconds=SEND_DELAY_SECONDS)
        return ["send_pitch"]

    if artifact.type == "calendar":
        # #11 — one content_draft per approved entry (approved_rows narrows it)
        entries = artifact.content.get("entries", [])
        chosen_entries = [entries[row] for row in approved_rows if 0 <= row < len(entries)] if approved_rows is not None else entries
        for entry in chosen_entries:
            await create_task(brand_id=artifact.brand_id, name="content_draft", queue="content", payload={"entry": entry})
        return ["content_draft"] * len(chosen_entries)

    if artifact.type == "plan":
        # an approved roadmap re-grounds the target query list (#4 re-run)
        await create_task(brand_id=artifact.brand_id, name="research_prompts", queue="research")
        # #25 — workstreams the Strategist marked as needing client-collected
        # data become bare client-contribution rows (no queue job; the daily
        # chase nags them; the study write-up rides content_draft once the
        # data arrives)
        study_workstreams = [workstream for workstream in artifact.content.get("workstreams", []) if workstream.get("needs_client_data")]
        for workstream in study_workstreams:
            Database.session.add(Task(brand_id=artifact.brand_id, name="collect_study_data", payload={"workstream": workstream.get("name")}))
        if study_workstreams:
            await Database.session.flush()
        return ["research_prompts"] + ["collect_study_data"] * len(study_workstreams)

    if artifact.type in ("gbp_touches", "reply"):
        # GBP work goes live via the GBP adapter, not the site engine
        await create_task(brand_id=artifact.brand_id, name="gbp_apply", queue="reputation", payload={"artifact_id": artifact.id})
        return ["gbp_apply"]

    if artifact.type in ("change_proposal", "draft"):
        operations = operations_for_artifact(artifact)
        if operations is None:
            # prose judgment proposals — approval recorded, a human applies
            logger.info(f"[approvals] artifact {artifact.id} ({artifact.type}) approved — no mechanical form, apply by hand")
            return []
        site = await Site.by_brand(artifact.brand_id)
        if site is None:
            logger.info(f"[approvals] artifact {artifact.id} approved but the brand has no connected site — nothing to apply")
            return []
        change_set, queued_work = await create_and_dispatch_change_set(artifact.id, site, artifact.brand_id, operations)
        return queued_work

    return []


def apply_lock(site_id: str) -> str:
    return f"site:{site_id}"


async def create_and_dispatch_change_set(artifact_id: str, site: Site, brand_id: str, operations: list[dict]) -> tuple[ChangeSet, list[str]]:
    """The one path a change set is born through — approval fan-out and revert
    both use it: durable row first, apply enqueued only when the platform's
    write adapter exists (parked pending otherwise)."""
    change_set = await ChangeSet.create(artifact_id=artifact_id, site_id=site.id, brand_id=brand_id, operations=operations)
    if site.platform not in APPLY_ENABLED_PLATFORMS:
        logger.info(f"[approvals] change set {change_set.id} recorded — {site.platform} write adapter not built yet, apply parked")
        return change_set, []
    # the per-site execution lock: applies to one site run one at a time, in approval order (interleaved read-before-write would corrupt rollback records)
    await create_task(brand_id=brand_id, name="change_set_apply", queue="changes", payload={"change_set_id": change_set.id}, lock=apply_lock(site.id))
    return change_set, ["change_set_apply"]
