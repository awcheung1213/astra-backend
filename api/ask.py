"""Ask — free-text questions, answered by the Analyst IN the request path.

The design doc's one exception to "api and worker talk only through Postgres":
the answer streams straight back. One bounded call does both halves — answer
what the assembled facts can support (receipts included), and flag when the
question actually needs a deep investigation; that flag chains a diagnosis
task (#23), whose result lands in Pulse.

NO AUTH YET (repo-wide) — same posture as the other routers."""

import json

from fastapi import APIRouter, HTTPException, status

from adapters.agents.claude_agent import AgentRefusal, ClaudeAgent
from objects.brand import Brand
from objects.task import Task
from schemas.ask_schemas import AskRequest, AskResponse
from utils.agent_json import parse_agent_json
from utils.agent_prompts import ROLE_ANALYST
from utils.task_queue import create_task
from worker.tasks.planning import assemble_diagnosis_facts

router = APIRouter(prefix="/api/v1")

QUESTION_MAX_CHARS = 2000


@router.post("/brands/{brand_id}/ask", response_model=AskResponse)
async def ask(brand_id: str, request: AskRequest) -> AskResponse:
    """One Analyst call over the same dated-timeline facts the diagnosis
    runner uses. When the facts can't settle the question, a diagnosis task
    is chained (skipped if one is already in flight) and the answer says so."""
    question = request.question.strip()
    if not question:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="question is empty")
    if len(question) > QUESTION_MAX_CHARS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"question exceeds {QUESTION_MAX_CHARS} characters")
    brand = await Brand.get(brand_id)

    facts = await assemble_diagnosis_facts(brand, trigger="ask", trigger_evidence={"question": question})
    try:
        agent_output = await ClaudeAgent().run_role(
            ROLE_ANALYST,
            prompt=_ask_prompt(brand, question, facts),
            tools=[],
            handlers={},
            operation="ask",
            organization_id=brand.organization_id,
            brand_id=brand.id,
        )
        response = parse_agent_json(agent_output)
    except AgentRefusal:
        # a refusal is a correct outcome, not a server error — and retrying
        # the same question would just refuse again, so no retry hint
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="I can't help with that request — ask me about your search and AI visibility")
    except ValueError:
        # non-JSON ramble — a fresh call usually complies
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="analyst answer malformed — retry")
    if not isinstance(response, dict) or not (isinstance(response.get("answer"), str) and response["answer"].strip()):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="analyst answer malformed — retry")

    diagnosis_queued = False
    if response.get("needs_diagnosis") is True and not await Task.in_flight(brand.id, "diagnosis"):
        # #23 — the deep dive the sync answer couldn't settle
        await create_task(brand_id=brand.id, name="diagnosis", queue="planning", payload={"trigger": "ask", "evidence": {"question": question}})
        diagnosis_queued = True
    return AskResponse.create({"answer": response["answer"], "diagnosis_queued": diagnosis_queued})


def _ask_prompt(brand: Brand, question: str, facts: dict) -> str:
    return (
        "The business owner asked the question below. Answer it in markdown, plainly and concretely, from\n"
        "the facts provided — tie claims to dated data points, and say honestly what the data cannot show.\n"
        'Set "needs_diagnosis" true ONLY when the question deserves a deep investigation the facts here\n'
        "cannot settle (a causal \"why\" with competing explanations) — your answer should then say what you\n"
        "CAN see and that a full diagnosis is coming.\n\n"
        "Respond with ONLY a JSON object, no prose and no code fences:\n"
        '{"answer": "markdown", "needs_diagnosis": false}\n\n'
        f"Brand: {brand.name} ({brand.website_url})\n\n"
        f"Question: {question}\n\n"
        f"Facts:\n{json.dumps(facts, indent=2, default=str)}"
    )
