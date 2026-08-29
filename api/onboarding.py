"""The onboarding flow — deliverables #1, #2, #7 and the kickoff fan-out of
#4/#5/#8 (see the deliverables index in README.md).

Not cron work: everything here fires ONCE per brand from API events. The UI
order is welcome -> org -> brand -> interview -> launch, so by launch time the
brand context is already extraction-filled and client-confirmed.

NO AUTH YET (repo-wide) — port utils/token_verifier.py + utils/authz.py from
geoviz-backend before these endpoints face the internet."""

import json
from datetime import datetime

from fastapi import APIRouter, HTTPException, status

from adapters.agents.claude_agent import ClaudeAgent
from objects.brand import Brand
from objects.task import STATUS_DONE, Task
from schemas.onboarding_schemas import InterviewRequest, InterviewResponse, LaunchResponse
from utils.agent_prompts import ROLE_ACCOUNT_MANAGER
from utils.database import Database
from utils.task_queue import create_task
from worker.tasks.briefs import CLIENT_TASK_NAMES

router = APIRouter(prefix="/api/v1")

# name -> queue: the internal work launch enqueues (#4, #5, #8)
KICKOFF_WORK = (
    ("prompt_research", "research"),
    ("competitor_bootstrap", "research"),
    ("plan_update", "planning"),
)


@router.post("/brands/{brand_id}/launch", response_model=LaunchResponse)
async def launch(brand_id: str) -> LaunchResponse:
    """Once per brand: create the client-contribution rows (#1/#7 — no queue
    job, the daily chase nags them) and enqueue the kickoff work (#4/#5/#8).
    409 on a second launch."""
    brand = await Brand.get(brand_id)
    if brand.onboarded_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="brand already launched")

    # bare rows, deliberately NOT create_task: these are completed by the
    # CLIENT, never a worker — a queue job would dead-letter (no runner by
    # these names). The row IS the mechanism: status queued = waiting on the
    # client (the daily chase nags), done = delivered.
    for client_task_name in sorted(CLIENT_TASK_NAMES):
        Database.session.add(Task(brand_id=brand.id, name=client_task_name))
    await Database.session.flush()

    for work_name, work_queue in KICKOFF_WORK:
        await create_task(brand_id=brand.id, name=work_name, queue=work_queue)

    # TODO(#6): the baseline is the normal weekly machinery, started early — the
    #       prompt sign-off endpoint (phase 3) defers submit_prompt_execution_batch
    #       so the first Sunday visibility_run scores a real baseline.
    # TODO(#7, config half): GA4 events + call-tracking config needs the Google
    #       adapter; until then only the client verification task row exists.

    await Brand.update(brand.id, onboarded_at=datetime.now())
    return LaunchResponse.create({"brand_id": brand.id, "client_tasks": sorted(CLIENT_TASK_NAMES), "queued_work": [work_name for work_name, _ in KICKOFF_WORK]})


@router.post("/brands/{brand_id}/interview", response_model=InterviewResponse)
async def interview(brand_id: str, request: InterviewRequest) -> InterviewResponse:
    """#2 — store the AM-conducted interview verbatim, extract the brand
    context from it (agent, no tools), and mark the answer_sme_interview
    client task done so the chase stops nagging. The response is the
    extraction for the client to confirm."""
    brand = await Brand.get(brand_id)

    extraction_json = await ClaudeAgent().run_role(
        ROLE_ACCOUNT_MANAGER,
        prompt=_extraction_prompt(brand, request.answers),
        tools=[],
        handlers={},
        operation="interview_extraction",
        organization_id=brand.organization_id,
        brand_id=brand.id,
    )
    context = _parse_extraction(extraction_json)

    await Brand.update(
        brand.id,
        interview_raw={**brand.interview_raw, **request.answers},
        description=context["description"] or brand.description,
        category=context["category"] or brand.category,
        audience=context["audience"] or brand.audience,
        differentiators=context["differentiators"] or brand.differentiators,
    )

    interview_task = await Task.first_queued(brand.id, "answer_sme_interview")
    if interview_task is not None:
        await Task.update_status(interview_task.id, STATUS_DONE)

    return InterviewResponse.create({"brand_id": brand.id, **context})


EXTRACTION_FIELDS = ("description", "category", "audience", "differentiators")


def _extraction_prompt(brand: Brand, answers: dict) -> str:
    return (
        "Below is a kickoff interview with the owner of the brand. Extract the brand context.\n"
        "Respond with ONLY a JSON object, no prose and no code fences:\n"
        '{"description": "...", "category": "...", "audience": "...", "differentiators": ["..."]}\n'
        'Use only what the owner actually said — leave a field empty ("" or []) if the interview does not cover it.\n\n'
        f"Brand: {brand.name} ({brand.website_url})\n\n"
        f"Interview:\n{json.dumps(answers, indent=2)}"
    )


def _parse_extraction(agent_output: str) -> dict:
    body = agent_output.strip()
    if body.startswith("```"):
        body = body.strip("`\n")
        body = body.removeprefix("json")
    try:
        context = json.loads(body)
    except json.JSONDecodeError:
        context = None
    if not isinstance(context, dict) or not all(field in context for field in EXTRACTION_FIELDS):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="interview extraction failed — retry")
    return {field: context[field] for field in EXTRACTION_FIELDS}
