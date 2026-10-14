"""Round 6 against a real database (run on evergreen_verify, never the dev DB):
the plan question is answered on its card and becomes the goal's starting
point, and the review-ask job retires the old templates for one copy-ready text."""

import uuid

from objects.artifact import STATUS_PENDING_REVIEW, STATUS_SUPERSEDED, Artifact
from objects.goal import Goal
from objects.task import STATUS_DONE, Task
from tests._auth import release_runner
from utils.database import Database, ReplicaDatabase
from utils.task_queue import create_task
from worker.tasks import reputation


async def _signup(client) -> str:
    await release_runner()
    payload = {"organization_name": f"test-org-{uuid.uuid4()}", "brand_name": f"Reyes Plumbing {uuid.uuid4().hex[:6]}", "website_url": "https://example.com", "footprint": "national"}
    return (await client.post("/api/v1/onboarding/signup", json=payload)).json()["brand_id"]


async def test_baseline_question_is_answered_on_the_card_and_sets_the_goal_start(client):
    brand_id = await _signup(client)
    async with ReplicaDatabase().connection(), Database().connection():
        task = Task(brand_id=brand_id, name="collect_study_data", payload={"workstream": "Set the measurement baseline"})
        Database.session.add(task)
        Database.session.add(Goal(brand_id=brand_id, metric="revenue", target=120000))
        await Database.session.commit()
        task_id = task.id

    feed = (await client.get(f"/api/v1/brands/{brand_id}/approvals")).json()
    card = next(row for row in feed["waiting_on_you"] if row["id"] == task_id)
    assert [ask["key"] for ask in card["asks"]] == ["monthly_revenue", "monthly_customers"]
    assert feed["gbp_connected"] is False

    missing = await client.post(f"/api/v1/brands/{brand_id}/client_tasks/{task_id}/answers", json={"answers": {"monthly_customers": "40"}})
    assert missing.status_code == 422
    saved = await client.post(f"/api/v1/brands/{brand_id}/client_tasks/{task_id}/answers", json={"answers": {"monthly_revenue": "$14,000", "monthly_customers": "40"}})
    assert saved.status_code == 200 and saved.json()["answers"] == {"monthly_revenue": 14000.0, "monthly_customers": 40.0}
    again = await client.post(f"/api/v1/brands/{brand_id}/client_tasks/{task_id}/answers", json={"answers": {"monthly_revenue": "1"}})
    assert again.status_code == 409

    roadmap = (await client.get(f"/api/v1/brands/{brand_id}/roadmap")).json()
    assert roadmap["goals"][0]["baseline"] == 14000.0
    assert roadmap["answers"]["Set the measurement baseline"][0] == {"question": "Money coming in per month right now", "value": 14000.0, "unit": "$ a month"}
    async with ReplicaDatabase().connection(), Database().connection():
        assert (await Task.get(task_id)).status == STATUS_DONE


async def test_review_ask_job_retires_templates_and_writes_one_text(client):
    brand_id = await _signup(client)
    async with ReplicaDatabase().connection(), Database().connection():
        old_task = await create_task(brand_id=brand_id, name="review_asks", queue="reputation")
        old = await Artifact.create(task_id=old_task.id, brand_id=brand_id, type="review_ask_templates", content={"sms": "Hi {first_name}"})
        await Database.session.commit()
        old_id, task_id = old.id, old_task.id
    await reputation.review_asks.func(task_id=task_id, brand_id=brand_id)
    async with ReplicaDatabase().connection(), Database().connection():
        assert (await Artifact.get(old_id)).status == STATUS_SUPERSEDED
        ask = await Artifact.latest(brand_id, type="review_ask")
        assert ask is not None and ask.status == STATUS_PENDING_REVIEW
        assert "{" not in ask.content["text"] and ask.content["link"].startswith("https://www.google.com/search?q=Reyes+Plumbing")
