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

Auth rides the router-level guard (utils/authz.py): signup requires only a
valid token (the caller has no org yet — signup is what claims one, exactly
once per user); interview/launch are brand-guarded by path."""

import json
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, status

from adapters.agents.claude_agent import ClaudeAgent
from constants.constants import MAX_FOOTPRINT_CITIES, US_STATE_ABBREVS
from objects.approval_setting import PRESET_AUTO_TASKS, ApprovalSetting
from objects.brand import FOOTPRINT_LOCAL, Brand
from objects.brand_market import BrandMarket
from objects.goal import Goal
from objects.market import Market
from objects.organization import Organization
from objects.task import STATUS_DONE, Task
from objects.user import User
from schemas.onboarding_schemas import InterviewRequest, InterviewResponse, LaunchRequest, LaunchResponse, SignupRequest, SignupResponse
from utils.agent_prompts import ROLE_ACCOUNT_MANAGER
from utils.authz import current_user
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
async def signup(request: SignupRequest, http_request: Request) -> SignupResponse:
    """Step 0 — the rows everything else hangs off: organization + brand
    (+ the goal when one was set), claimed by the signed-in caller (a user
    belongs to exactly ONE org). Re-entry with an unlaunched brand hands the
    existing ids back instead of 409ing, so a wizard resumed on a fresh
    device (no sessionStorage) doesn't strand at step 3; a launched brand
    means the caller shouldn't be in the wizard at all — 409."""
    user = current_user(http_request)
    if user.organization_id is not None:
        existing_brands = await Brand.by_organization_id(user.organization_id)
        unlaunched = [brand for brand in existing_brands if brand.onboarded_at is None]
        if not unlaunched:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="you already have an organization")
        return SignupResponse.create({"organization_id": user.organization_id, "brand_id": unlaunched[0].id})

    brand_name = request.brand_name.strip()
    website_url = request.website_url.strip()
    if not brand_name or not website_url:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="brand_name and website_url are required")
    if not website_url.startswith("http"):
        website_url = f"https://{website_url}"
    # a local brand with no markets would silently collect nothing forever —
    # validate the city list up front (parse errors 422 before any row lands)
    city_pairs = _parse_cities(request.cities or []) if request.footprint == FOOTPRINT_LOCAL else []

    organization = await Organization.create(name=request.organization_name.strip() or brand_name)
    brand = Brand(organization_id=organization.id, name=brand_name, website_url=website_url, footprint=request.footprint)
    Database.session.add(brand)
    await Database.session.flush()
    for city, state in city_pairs:
        market = await Market.get_or_create_city(city, state)
        await BrandMarket.subscribe(brand.id, market.id)
    if request.goal_metric and request.goal_target:
        Database.session.add(Goal(brand_id=brand.id, metric=request.goal_metric, target=request.goal_target))
        await Database.session.flush()
    await User.claim_organization(user.id, organization.id)
    return SignupResponse.create({"organization_id": organization.id, "brand_id": brand.id})


def _parse_cities(cities: list[str]) -> list[tuple[str, str]]:
    """'City, ST' strings -> deduped (city, state) pairs, loudly validated:
    the wizard's typeahead makes these picked-not-typed in the common case,
    so anything malformed here is worth stopping the signup over."""
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in cities:
        if "," not in raw:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"'{raw}' must be 'City, ST'")
        city_part, state_part = raw.rsplit(",", 1)
        city, state = city_part.strip(), state_part.strip().upper()
        if not city or state not in US_STATE_ABBREVS:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"'{raw}' must be 'City, ST' with a real state code")
        key = (city.lower(), state)
        if key in seen:
            continue
        seen.add(key)
        pairs.append((city, state))
    if not pairs:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="footprint is local — pick at least one city")
    if len(pairs) > MAX_FOOTPRINT_CITIES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"at most {MAX_FOOTPRINT_CITIES} cities")
    return pairs


@router.post("/brands/{brand_id}/launch", response_model=LaunchResponse)
async def launch(brand_id: str, request: LaunchRequest | None = None) -> LaunchResponse:
    """Once per brand: record the goal and the approval preset (both chosen
    after signup), create the client-contribution rows (#1/#7 — no queue job,
    the daily chase nags them), and enqueue launch's own kickoff work (#3/#8 —
    the research pair is the interview's, never launch's). 409 on a second
    launch."""
    brand = await Brand.get(brand_id)
    if brand.onboarded_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="brand already launched")
    approval_preset = request.approval_preset if request else None
    if approval_preset is not None and approval_preset not in PRESET_AUTO_TASKS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"approval_preset must be one of {sorted(PRESET_AUTO_TASKS)}")

    if request and request.goal_metric and request.goal_target:
        Database.session.add(Goal(brand_id=brand.id, metric=request.goal_metric, target=request.goal_target))
        await Database.session.flush()
    if approval_preset is not None:
        await ApprovalSetting.apply_preset(brand.id, approval_preset)

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

    await Brand.update(brand.id, onboarded_at=datetime.now(), approval_preset=approval_preset or brand.approval_preset)
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
