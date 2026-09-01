"""The onboarding flow — deliverables #1, #2, #7 and the kickoff fan-out of
#4/#5/#8 (see the deliverables index in README.md).

Not cron work: everything here fires ONCE per brand from API events. The UI
creates resources as it learns enough to: signup fires once coverage is known
(name + site + footprint), the interview submit follows immediately, and
launch closes the flow with the goal. Each enqueue has exactly ONE owner, so
no dedupe checks are needed: the interview owns the research pair (research
grounds in the extraction, so it fires when the interview lands — at signup
time normally, or later from Tasks if it was skipped, which the daily chase
nags for), and launch owns plan_update + site_audit + the client-task rows,
single-shot behind its 409.

NO AUTH YET (repo-wide) — port utils/token_verifier.py + utils/authz.py from
geoviz-backend before these endpoints face the internet."""

import json
from datetime import datetime

from fastapi import APIRouter, HTTPException, status

from adapters.agents.claude_agent import ClaudeAgent
from objects.brand import Brand
from objects.goal import Goal
from objects.organization import Organization
from objects.task import STATUS_DONE, Task
from schemas.onboarding_schemas import InterviewRequest, InterviewResponse, LaunchRequest, LaunchResponse, SignupRequest, SignupResponse
from utils.agent_prompts import ROLE_ACCOUNT_MANAGER
from utils.database import Database
from utils.task_queue import create_task

router = APIRouter(prefix="/api/v1")

# name -> queue, one owner each. The interview enqueues the research pair
# (its extraction is exactly what the researchers ground in, and firing them
# there puts real competitors + questions on the wizard's review step);
# launch enqueues the rest (#8, and the first #3 sweep — the crawler needs
# only website_url, no connected site).
RESEARCH_WORK = (
    ("prompt_research", "research"),
    ("competitor_bootstrap", "research"),
)
LAUNCH_WORK = (
    ("plan_update", "planning"),
    ("site_audit", "audit"),
)

# launch's slice of briefs.CLIENT_TASK_NAMES (the chase vocabulary)
ONBOARDING_CLIENT_TASKS = ("answer_sme_interview", "grant_access", "upload_photos", "verify_conversion_tracking")


@router.post("/onboarding/signup", response_model=SignupResponse)
async def signup(request: SignupRequest) -> SignupResponse:
    """Step 0 — the rows everything else hangs off: organization + brand
    (+ the goal when one was set). NO AUTH YET: no user/membership rows —
    those arrive with the auth port."""
    brand_name = request.brand_name.strip()
    website_url = request.website_url.strip()
    if not brand_name or not website_url:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="brand_name and website_url are required")
    if not website_url.startswith("http"):
        website_url = f"https://{website_url}"

    organization = await Organization.create(name=request.organization_name.strip() or brand_name)
    brand = Brand(organization_id=organization.id, name=brand_name, website_url=website_url, footprint=request.footprint)
    Database.session.add(brand)
    await Database.session.flush()
    if request.goal_metric and request.goal_target:
        Database.session.add(Goal(brand_id=brand.id, metric=request.goal_metric, target=request.goal_target))
        await Database.session.flush()
    return SignupResponse.create({"organization_id": organization.id, "brand_id": brand.id})


@router.post("/brands/{brand_id}/launch", response_model=LaunchResponse)
async def launch(brand_id: str, request: LaunchRequest | None = None) -> LaunchResponse:
    """Once per brand: record the goal (chosen after signup), create the
    client-contribution rows (#1/#7 — no queue job, the daily chase nags
    them), and enqueue launch's own kickoff work (#3/#8 — the research pair
    is the interview's, never launch's). 409 on a second launch."""
    brand = await Brand.get(brand_id)
    if brand.onboarded_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="brand already launched")

    if request and request.goal_metric and request.goal_target:
        Database.session.add(Goal(brand_id=brand.id, metric=request.goal_metric, target=request.goal_target))
        await Database.session.flush()

    # bare rows, deliberately NOT create_task: these are completed by the
    # CLIENT, never a worker — a queue job would dead-letter (no runner by
    # these names). The row IS the mechanism: status queued = waiting on the
    # client (the daily chase nags), done = delivered. Launch creates its OWN
    # subset of the chase vocabulary — other producers (plan approval's
    # collect_study_data) add theirs when the work actually calls for it.
    # answer_sme_interview is skipped when the interview already happened —
    # launch must never nag for what the wizard collected minutes earlier.
    client_tasks_created = []
    for client_task_name in ONBOARDING_CLIENT_TASKS:
        if client_task_name == "answer_sme_interview" and brand.interview_raw:
            continue
        Database.session.add(Task(brand_id=brand.id, name=client_task_name))
        client_tasks_created.append(client_task_name)
    await Database.session.flush()

    for work_name, work_queue in LAUNCH_WORK:
        await create_task(brand_id=brand.id, name=work_name, queue=work_queue)

    # #6 — the baseline is the normal weekly machinery started early: the
    #       prompt sign-off endpoint (api/prompts.py) defers the first fill
    #       once the human activates prompts; Sunday's visibility_run scores it.
    # TODO(#7, config half): GA4 events + call-tracking config needs the Google
    #       adapter; until then only the client verification task row exists.

    await Brand.update(brand.id, onboarded_at=datetime.now())
    return LaunchResponse.create({"brand_id": brand.id, "client_tasks": client_tasks_created, "queued_work": [work_name for work_name, _ in LAUNCH_WORK]})


@router.post("/brands/{brand_id}/interview", response_model=InterviewResponse)
async def interview(brand_id: str, request: InterviewRequest) -> InterviewResponse:
    """#2 — store the AM-conducted interview verbatim, extract the brand
    context from it (agent, no tools), mark the answer_sme_interview client
    task done so the chase stops nagging, and kick off the research pair —
    the extraction is exactly what competitor_bootstrap and prompt_research
    ground in, and firing them here (not at launch) is what puts real
    competitors + questions on the wizard's review step. The response is the
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

    for work_name, work_queue in RESEARCH_WORK:
        await create_task(brand_id=brand.id, name=work_name, queue=work_queue)

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
