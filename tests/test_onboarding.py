"""The onboarding flow's one-owner enqueue contract: signup once coverage is
known; the interview submit fills the brand context AND fires the research
pair (it alone owns them — at signup time normally, later from Tasks if it
was skipped); launch records the goal, enqueues only its own work
(plan_update + site_audit), and never nags for an interview that already
happened. ClaudeAgent.run_role is replaced with a canned extraction — no
Anthropic call is ever made. Runs in compose (make test), needs the
database."""

import json
import uuid

from adapters.agents.claude_agent import ClaudeAgent

EXTRACTION = {"description": "Sells nail polish online.", "category": "Beauty & cosmetics", "audience": "Online nail polish buyers", "differentiators": ["Natural ingredients"]}


def _stub_extraction(monkeypatch):
    async def run_role(self, role, **kwargs):
        return json.dumps(EXTRACTION)

    monkeypatch.setattr(ClaudeAgent, "run_role", run_role)


async def _signup(client, **overrides):
    payload = {"organization_name": f"test-org-{uuid.uuid4()}", "brand_name": f"test brand {uuid.uuid4()}", "website_url": "https://example.com", "footprint": "national", **overrides}
    response = await client.post("/api/v1/onboarding/signup", json=payload)
    assert response.status_code == 200
    return response.json()


async def _interview(client, brand_id):
    answers = {"What does the business do?": "We sell nail polish online.", "Dream customer?": "Sephora shoppers.", "Proof points?": "Natural ingredients, high quality."}
    return await client.post(f"/api/v1/brands/{brand_id}/interview", json={"answers": answers})


async def test_interview_extracts_and_fires_the_research_pair(client, monkeypatch):
    _stub_extraction(monkeypatch)
    brand_id = (await _signup(client))["brand_id"]

    response = await _interview(client, brand_id)
    assert response.status_code == 200
    body = response.json()
    assert body["description"] == EXTRACTION["description"]
    assert body["differentiators"] == EXTRACTION["differentiators"]

    worklog = (await client.get(f"/api/v1/brands/{brand_id}/worklog")).json()
    queued_names = {task["name"] for task in worklog["queued"]} | {task["name"] for task in worklog["running"]}
    assert {"prompt_research", "competitor_bootstrap"} <= queued_names


async def test_launch_after_interview_skips_the_answered_interview(client, monkeypatch):
    _stub_extraction(monkeypatch)
    brand_id = (await _signup(client))["brand_id"]
    await _interview(client, brand_id)

    response = await client.post(f"/api/v1/brands/{brand_id}/launch", json={"goal_metric": "revenue", "goal_target": 120000})
    assert response.status_code == 200
    body = response.json()
    assert "answer_sme_interview" not in body["client_tasks"]
    assert "grant_access" in body["client_tasks"]
    # the research pair is the interview's — launch only ever adds its own work
    assert body["queued_work"] == ["plan_update", "site_audit"]

    goals = (await client.get(f"/api/v1/brands/{brand_id}/roadmap")).json()["goals"]
    assert goals and goals[0]["metric"] == "revenue" and goals[0]["target"] == 120000

    # nothing waits on the interview
    approvals = (await client.get(f"/api/v1/brands/{brand_id}/approvals")).json()
    assert "answer_sme_interview" not in {task["name"] for task in approvals["waiting_on_you"]}

    second = await client.post(f"/api/v1/brands/{brand_id}/launch")
    assert second.status_code == 409


async def test_skipped_interview_waits_and_the_late_answer_fires_research(client, monkeypatch):
    _stub_extraction(monkeypatch)
    brand_id = (await _signup(client))["brand_id"]

    response = await client.post(f"/api/v1/brands/{brand_id}/launch")
    assert response.status_code == 200
    body = response.json()
    assert "answer_sme_interview" in body["client_tasks"]
    # research strictly waits for the interview it grounds in
    assert body["queued_work"] == ["plan_update", "site_audit"]

    # answering later (from Tasks) closes the chase row and fires research
    assert (await _interview(client, brand_id)).status_code == 200
    approvals = (await client.get(f"/api/v1/brands/{brand_id}/approvals")).json()
    assert "answer_sme_interview" not in {task["name"] for task in approvals["waiting_on_you"]}
    worklog = (await client.get(f"/api/v1/brands/{brand_id}/worklog")).json()
    queued_names = {task["name"] for task in worklog["queued"]} | {task["name"] for task in worklog["running"]}
    assert {"prompt_research", "competitor_bootstrap"} <= queued_names


async def test_competitor_review_add_list_remove(client):
    signup = await _signup(client)
    brand_id = signup["brand_id"]

    added = await client.post(f"/api/v1/brands/{brand_id}/competitors", json={"name": "Essie"})
    assert added.status_code == 200
    competitor = added.json()

    listed = (await client.get(f"/api/v1/brands/{brand_id}/competitors")).json()
    assert [row["name"] for row in listed["competitors"]] == ["Essie"]

    removed = await client.delete(f"/api/v1/brands/{brand_id}/competitors/{competitor['id']}")
    assert removed.status_code == 200 and removed.json()["is_excluded"] is True

    assert (await client.get(f"/api/v1/brands/{brand_id}/competitors")).json()["competitors"] == []

    # re-adding an excluded name un-excludes it (the human overrides)
    readded = await client.post(f"/api/v1/brands/{brand_id}/competitors", json={"name": "Essie"})
    assert readded.status_code == 200
    assert [row["name"] for row in (await client.get(f"/api/v1/brands/{brand_id}/competitors")).json()["competitors"]] == ["Essie"]


async def test_competitor_add_rejects_the_own_brand(client):
    signup = await _signup(client, brand_name="Lippmann Collection")
    response = await client.post(f"/api/v1/brands/{signup['brand_id']}/competitors", json={"name": "Lippmann"})
    assert response.status_code == 422
