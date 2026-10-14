"""Answering what a plan step needs from the owner, on the card itself.

  POST /brands/{id}/client_tasks/{task_id}/answers   {"answers": {key: value}}

The questions are the task's asks (utils/study_asks.py). A saved answer
closes the task; "money coming in per month right now" also becomes the
starting point on his revenue goal, so the plan reads "$X today -> goal".
Auth rides the router-level guard (brand_id -> its org); the task must be
that brand's."""

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from objects.goal import METRIC_REVENUE, Goal
from objects.task import STATUS_DONE, STATUS_QUEUED, Task
from utils.database import Database
from utils.study_asks import KEY_MONTHLY_REVENUE, asks_for, clean_answers

router = APIRouter(prefix="/api/v1")

ANSWERABLE_TASKS = {"collect_study_data"}


class AnswersRequest(BaseModel):
    answers: dict


@router.post("/brands/{brand_id}/client_tasks/{task_id}/answers")
async def answer_client_task(brand_id: str, task_id: str, request: AnswersRequest) -> dict:
    """Store the answers on the task and close it. 404 for another brand's
    task, 409 once answered, 422 with the problems for a missing required
    answer or a non-number where a number goes."""
    task = await Task.get(task_id)
    if task.brand_id != brand_id or task.name not in ANSWERABLE_TASKS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such question for this brand")
    if task.status != STATUS_QUEUED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="already answered")
    asks = asks_for(task.payload)
    answers, problems = clean_answers(asks, request.answers or {})
    if problems:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="; ".join(problems))

    task.payload = {**(task.payload or {}), "asks": asks, "answers": answers}
    Database.session.add(task)
    await Database.session.flush()
    await Task.update_status(task.id, STATUS_DONE)
    if KEY_MONTHLY_REVENUE in answers:
        await Goal.set_baseline(brand_id, METRIC_REVENUE, answers[KEY_MONTHLY_REVENUE])
    return {"task_id": task.id, "status": STATUS_DONE, "answers": answers}
