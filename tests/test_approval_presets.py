"""Approval presets: the macro REPLACES a brand's approval_settings rows
(the enforcement truth) while brands.approval_preset stores display truth;
initial_artifact_status consults the floor first, then the producing task's
row; and an artifact born auto_approved dispatches its fan-out at birth —
the change set exists before any human looks. Runs in compose (make test),
needs the database; worker must be stopped (fan-out enqueues real jobs)."""

import uuid

from sqlalchemy import select

from adapters.cms.wordpress_adapter import WordPressAdapter
from objects.approval_setting import MODE_AUTO, ApprovalSetting
from objects.artifact import STATUS_AUTO_APPROVED, STATUS_PENDING_REVIEW, Artifact
from objects.brand import Brand
from objects.change_set import ChangeSet
from objects.task import Task
from tests._auth import release_runner
from utils.approval_flow import fan_out_approval, initial_artifact_status
from utils.database import Database, ReplicaDatabase

WP_CREDENTIALS = {"base_url": "sunrisedental.example", "username": "astra", "app_password": "abcd efgh"}


async def _signup(client) -> str:
    await release_runner()  # one org per user — free the runner so this signup claims fresh
    payload = {"organization_name": f"test-org-{uuid.uuid4()}", "brand_name": f"test brand {uuid.uuid4()}", "website_url": "https://example.com", "footprint": "national"}
    return (await client.post("/api/v1/onboarding/signup", json=payload)).json()["brand_id"]


async def _settings_rows(brand_id: str) -> dict[str, str]:
    async with ReplicaDatabase().connection(), Database().connection():
        rows = await ReplicaDatabase.session.execute(select(ApprovalSetting).where(ApprovalSetting.brand_id == brand_id))
        return {setting.task_name: setting.mode for setting in rows.scalars().all()}


async def test_preset_rides_launch(client):
    brand_id = await _signup(client)
    response = await client.post(f"/api/v1/brands/{brand_id}/launch", json={"approval_preset": "balanced"})
    assert response.status_code == 200

    assert await _settings_rows(brand_id) == {"technical_fix": MODE_AUTO}
    async with ReplicaDatabase().connection(), Database().connection():
        assert (await Brand.get(brand_id)).approval_preset == "balanced"

    # a made-up preset stops the launch before anything lands
    fresh_brand_id = await _signup(client)
    assert (await client.post(f"/api/v1/brands/{fresh_brand_id}/launch", json={"approval_preset": "yolo"})).status_code == 422


async def test_reset_preset_replaces_rows(client):
    brand_id = await _signup(client)
    assert (await client.post(f"/api/v1/brands/{brand_id}/launch", json={"approval_preset": "hands_off"})).status_code == 200
    assert await _settings_rows(brand_id) == {"technical_fix": MODE_AUTO, "content_draft": MODE_AUTO, "content_calendar": MODE_AUTO}

    # "relax it after the first month" in reverse: tightening back up deletes
    # every auto row — absence means reviewed
    response = await client.put(f"/api/v1/brands/{brand_id}/approval_preset", json={"approval_preset": "review_everything"})
    assert response.status_code == 200 and response.json()["auto_approved_tasks"] == []
    assert await _settings_rows(brand_id) == {}

    response = await client.put(f"/api/v1/brands/{brand_id}/approval_preset", json={"approval_preset": "balanced"})
    assert response.status_code == 200 and response.json()["auto_approved_tasks"] == ["technical_fix"]
    assert await _settings_rows(brand_id) == {"technical_fix": MODE_AUTO}

    assert (await client.put(f"/api/v1/brands/{brand_id}/approval_preset", json={"approval_preset": "yolo"})).status_code == 422

    # display truth follows — the PATCH surface shows the active preset
    patched = (await client.patch(f"/api/v1/brands/{brand_id}", json={})).json()
    assert patched["approval_preset"] == "balanced"


async def test_born_status_floor_then_rows(client):
    brand_id = await _signup(client)
    assert (await client.post(f"/api/v1/brands/{brand_id}/launch", json={"approval_preset": "hands_off"})).status_code == 200

    async with ReplicaDatabase().connection(), Database().connection():
        # floor types never auto-ship, even under the loosest preset
        assert await initial_artifact_status(brand_id, "review_response", "reply") == STATUS_PENDING_REVIEW
        assert await initial_artifact_status(brand_id, "gbp_management", "gbp_touches") == STATUS_PENDING_REVIEW
        # preset rows carry the reversible tier
        assert await initial_artifact_status(brand_id, "technical_fix", "change_proposal") == STATUS_AUTO_APPROVED
        assert await initial_artifact_status(brand_id, "content_draft", "draft") == STATUS_AUTO_APPROVED
        # tasks the macro doesn't list default to reviewed
        assert await initial_artifact_status(brand_id, "content_refresh", "draft") == STATUS_PENDING_REVIEW
        assert await initial_artifact_status(brand_id, "technical_judgment", "change_proposal") == STATUS_PENDING_REVIEW


async def test_auto_born_change_proposal_dispatches_at_birth(client, monkeypatch):
    async def verify(self):
        pass

    monkeypatch.setattr(WordPressAdapter, "verify", verify)
    brand_id = await _signup(client)
    assert (await client.post(f"/api/v1/brands/{brand_id}/launch", json={"approval_preset": "balanced"})).status_code == 200
    assert (await client.post(f"/api/v1/brands/{brand_id}/site", json={"platform": "wordpress", "credentials": WP_CREDENTIALS})).status_code == 200

    # simulate the technical_fix producer's birth path (the worker is stopped
    # in tests; the runner itself would fetch live pages)
    async with ReplicaDatabase().connection(), Database().connection():
        producer_task = Task(brand_id=brand_id, name="technical_fix")
        Database.session.add(producer_task)
        await Database.session.flush()
        artifact_status = await initial_artifact_status(brand_id, "technical_fix", "change_proposal")
        assert artifact_status == STATUS_AUTO_APPROVED
        artifact = await Artifact.create(task_id=producer_task.id, brand_id=brand_id, type="change_proposal", content={"operations": [{"field": "title", "url": "https://example.com", "value": "Better title"}]}, status=artifact_status)
        assert await fan_out_approval(artifact) == ["change_set_apply"]
        await Database.session.commit()
        artifact_id = artifact.id

    async with ReplicaDatabase().connection(), Database().connection():
        rows = await ReplicaDatabase.session.execute(select(ChangeSet).where(ChangeSet.artifact_id == artifact_id))
        assert len(list(rows.scalars().all())) == 1

    # the born-approved artifact never appears in the human queue, but the
    # dispatched apply shows up as queued work
    approvals = (await client.get(f"/api/v1/brands/{brand_id}/approvals")).json()
    assert artifact_id not in {pending["id"] for pending in approvals["pending"]}
    worklog = (await client.get(f"/api/v1/brands/{brand_id}/worklog")).json()
    assert "change_set_apply" in {task["name"] for task in worklog["queued"]}
