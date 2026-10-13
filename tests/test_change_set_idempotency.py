"""BUG-028 (crash between site write and marker keeps the ORIGINAL rollback
record; publish never duplicates), BUG-029 (verdicts lock the artifact row),
BUG-031 (resumed-apply notification counts the set), BUG-032 (per-site apply
lock reaches procrastinate), BUG-033 (attempt_count survives a failed
attempt) — pure unit tests, no DB, no client fixture."""

import copy
from contextlib import asynccontextmanager, nullcontext
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from adapters.cms.common import CmsWriteError, no_checkpoint
from adapters.cms.shopify_adapter import ShopifyAdapter
from adapters.cms.wordpress_adapter import WordPressAdapter
from api import approvals as approvals_api
from objects.artifact import STATUS_APPROVED, Artifact
from objects.change_set import ChangeSet, merge_operation_progress
from objects.task import STATUS_FAILED, Task
from schemas.approval_schemas import ReviewRequest
from utils import approval_flow, task_queue
from utils.database import Database
from worker.tasks import changes


class Crash(Exception):
    """The worker dies after the site write, before the marker commit."""


class FakeSession:
    """Just enough AsyncSession: rows by (model, id); commit snapshots each
    row's state, rollback restores the last commit — what a real crash or a
    task_lifecycle rollback leaves behind."""

    def __init__(self, *rows) -> None:
        self.rows = {(type(row), row.id): row for row in rows}
        self.executed: list = []
        self._snapshot()

    async def get(self, model, id):
        return self.rows.get((model, id))

    def add(self, row) -> None:
        pass

    async def flush(self) -> None:
        pass

    def begin_nested(self):
        return nullcontext()

    async def refresh(self, row) -> None:
        pass

    async def close(self) -> None:
        pass

    async def execute(self, statement, params=None):
        self.executed.append((statement, params))
        return None

    def _snapshot(self) -> None:
        self.snapshot = {key: copy.deepcopy({attribute: value for attribute, value in row.__dict__.items() if not attribute.startswith("_sa_")}) for key, row in self.rows.items()}

    async def commit(self) -> None:
        self._snapshot()

    async def rollback(self) -> None:
        for key, row in self.rows.items():
            for attribute, value in copy.deepcopy(self.snapshot[key]).items():
                setattr(row, attribute, value)


@pytest.fixture
def engine(monkeypatch):
    """change_set_apply over a fake session + a fake site (a dict of field values)."""
    state = SimpleNamespace(site={"title": "Original"}, writes=[], crash_after_write=set(), fail_before_write=set(), notifications=[], creates=0)

    class FakeAdapter:
        def __init__(self, site) -> None:
            pass

        async def apply(self, operation: dict, checkpoint=no_checkpoint) -> dict:
            if operation["id"] in state.fail_before_write:
                state.fail_before_write.discard(operation["id"])
                raise CmsWriteError("bad target")
            if operation["op"] == "publish_page":
                if operation.get("created_ref"):
                    return {"previous_value": None, "created_ref": operation["created_ref"]}
                state.creates += 1
                await checkpoint({"created_ref": f"page-{state.creates}"})
                result = {"previous_value": None, "created_ref": f"page-{state.creates}"}
            else:
                previous_value = state.site[operation["field"]]  # read-before-write, EVERY attempt
                await checkpoint({"previous_value": previous_value})
                state.site[operation["field"]] = operation["value"]
                state.writes.append(operation["id"])
                result = {"previous_value": previous_value}
            if operation["id"] in state.crash_after_write:
                state.crash_after_write.discard(operation["id"])
                raise Crash()
            return result

    @asynccontextmanager
    async def fake_lifecycle(task_id):
        try:
            yield SimpleNamespace(payload={"change_set_id": "cs1"})
        except Exception:
            await Database.session.rollback()
            raise

    async def fake_notify(**kwargs):
        state.notifications.append((kwargs["title"], kwargs["body"]))

    async def fake_brand(brand_id):
        return SimpleNamespace(organization_id="org1")

    async def fake_site(site_id):
        return SimpleNamespace(platform="wordpress")

    def arrange(operations: list[dict]) -> ChangeSet:
        change_set = ChangeSet(id="cs1", artifact_id="a1", site_id="s1", brand_id="b1", status="pending", operations=operations)
        state.session = FakeSession(change_set)
        monkeypatch.setattr(Database, "get_session", lambda self: state.session)
        return change_set

    monkeypatch.setattr(changes, "task_lifecycle", fake_lifecycle)
    monkeypatch.setattr(changes, "ADAPTER_BY_PLATFORM", {"wordpress": FakeAdapter})
    monkeypatch.setattr(changes.Notification, "create", staticmethod(fake_notify))

    async def no_unread_notice(organization_id, since):
        return None  # every apply in these tests starts a fresh notice

    monkeypatch.setattr(changes.Notification, "unread_change_notice", staticmethod(no_unread_notice))
    monkeypatch.setattr(changes.Brand, "get", staticmethod(fake_brand))
    monkeypatch.setattr(changes.Site, "get", staticmethod(fake_site))

    async def not_a_revert(change_set_id):
        return None

    monkeypatch.setattr(changes, "_reverted_original", not_a_revert)  # BUG-021: the apply path asks whether the set is an Undo
    state.arrange = arrange
    return state


def _set_title(index: int, value: str) -> dict:
    return changes._operation(index, "set_field", target="https://example.com/", field="title", value=value)


##############################
# BUG-028: crash between write and marker
##############################


async def test_crash_after_write_keeps_the_original_rollback_record(engine):
    change_set = engine.arrange([_set_title(0, "New")])
    engine.crash_after_write.add("op-0")
    with pytest.raises(Crash):
        await changes.change_set_apply.func(task_id="t1", brand_id="b1")
    assert engine.site["title"] == "New" and not change_set.operations[0].get("completed_at")  # written, marker lost

    await changes.change_set_apply.func(task_id="t1", brand_id="b1")  # the retry re-reads "New"...

    operation = change_set.operations[0]
    assert operation["completed_at"] and operation["previous_value"] == "Original"  # ...but keeps the first capture
    assert changes.invert_operations(change_set)[0]["value"] == "Original"  # Undo puts the site back


async def test_crash_after_publish_never_creates_a_second_page(engine):
    change_set = engine.arrange([changes._operation(0, "publish_page", target=None, value={"title": "T", "meta_description": "M", "body_markdown": "B"})])
    engine.crash_after_write.add("op-0")
    with pytest.raises(Crash):
        await changes.change_set_apply.func(task_id="t1", brand_id="b1")
    await changes.change_set_apply.func(task_id="t1", brand_id="b1")
    assert engine.creates == 1 and change_set.operations[0]["created_ref"] == "page-1"


def test_previous_value_is_captured_once_even_when_none():
    operation = _set_title(0, "New")
    captured = merge_operation_progress(operation, {"previous_value": None})
    assert captured["previous_captured_at"] and captured["previous_value"] is None
    assert merge_operation_progress(captured, {"previous_value": "New"})["previous_value"] is None
    assert merge_operation_progress({**operation, "created_ref": "7"}, {"created_ref": "8"})["created_ref"] == "7"


async def test_wordpress_checkpoints_before_every_write_and_never_republishes():
    events: list = []
    adapter = WordPressAdapter.__new__(WordPressAdapter)

    async def fake_request(client, method, path, json_body=None):
        events.append((method, path))
        return {"id": 42, "status": "publish"}

    async def checkpoint(fields):
        events.append(("checkpoint", fields))

    adapter._request = fake_request  # type: ignore[method-assign]
    page = {"id": 7, "title": {"raw": "Old"}, "excerpt": {"raw": ""}, "content": {"raw": "<p>old</p>"}}
    await adapter._set_field(None, "pages", page, "h1", "New", checkpoint)
    assert events == [("checkpoint", {"previous_value": "Old"}), ("POST", "/wp-json/wp/v2/pages/7")]

    events.clear()
    await adapter._update_page(None, "pages", page, {"title": "N", "meta_description": "M", "body_markdown": "B"}, checkpoint)
    assert events[0][0] == "checkpoint" and events[1][0] == "POST"

    events.clear()
    assert await adapter._publish_page(None, {"title": "T", "meta_description": "M", "body_markdown": "B"}, None, checkpoint) == {"previous_value": None, "created_ref": "42"}
    assert events == [("POST", "/wp-json/wp/v2/pages"), ("checkpoint", {"created_ref": "42"})]

    events.clear()
    assert (await adapter._publish_page(None, {"title": "T", "meta_description": "M", "body_markdown": "B"}, "42", checkpoint))["created_ref"] == "42"
    assert events == []  # a retry of a publish that already created its page touches nothing


async def test_shopify_checkpoints_before_either_write_and_never_republishes():
    events: list = []
    adapter = ShopifyAdapter.__new__(ShopifyAdapter)

    async def fake_request(client, method, path, json_body=None):
        events.append((method, path))
        if path.endswith("metafields.json?namespace=global&key=description_tag"):
            return {"metafields": [{"id": 9, "value": "Old desc"}]}
        return {"page": {"id": 55}}

    async def checkpoint(fields):
        events.append(("checkpoint", fields))

    adapter._request = fake_request  # type: ignore[method-assign]
    page = {"id": 7, "title": "Old", "body_html": "<p>old</p>"}
    await adapter._update_page(None, page, {"title": "N", "meta_description": "M", "body_markdown": "B"}, checkpoint)
    kinds = [event[0] for event in events]
    assert kinds == ["GET", "checkpoint", "PUT", "PUT"] and events[1][1]["previous_value"]["meta_description"] == "Old desc"

    events.clear()
    await adapter._publish_page(None, {"title": "T", "meta_description": "M", "body_markdown": "B"}, "55", checkpoint)
    assert ("POST", "/pages.json") not in events  # page exists: only its metafield is finished


##############################
# BUG-031: resumed apply reports the whole set
##############################


async def test_resumed_apply_notification_counts_every_completed_op(engine):
    engine.site["title"] = "Original"
    change_set = engine.arrange([_set_title(0, "A"), _set_title(1, "B")])
    engine.fail_before_write.add("op-1")
    with pytest.raises(CmsWriteError):
        await changes.change_set_apply.func(task_id="t1", brand_id="b1")
    await changes.change_set_apply.func(task_id="t1", brand_id="b1")
    assert engine.writes == ["op-0", "op-1"]  # op-0 not repeated
    # the SET is described, not this attempt's share of it (marco-review T4: what changed and where)
    assert engine.notifications == [("Made 2 changes to your homepage", "Live now on your site. Tap to see what changed.")] and change_set.status == "applied"


##############################
# BUG-029: verdicts serialize on the artifact row
##############################


async def test_artifact_verdict_reads_the_row_for_update(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(Database, "get_session", lambda self: session)
    with pytest.raises(Exception):
        await Artifact.get_for_update("a1")  # fake execute returns nothing; only the statement matters
    statement = session.executed[0][0]
    assert "FOR UPDATE" in str(statement.compile(dialect=postgresql.dialect()))


async def test_review_loser_sees_the_winners_verdict_and_409s(monkeypatch):
    calls: list = []

    async def locked_read(artifact_id):
        calls.append(artifact_id)
        return SimpleNamespace(id=artifact_id, status=STATUS_APPROVED, brand_id="b1", type="change_proposal")  # the winner committed first

    async def must_not_run(*args, **kwargs):
        raise AssertionError("a losing verdict must not fan out")

    monkeypatch.setattr(approvals_api.Artifact, "get_for_update", staticmethod(locked_read))
    monkeypatch.setattr(approvals_api.Artifact, "review", staticmethod(must_not_run))
    monkeypatch.setattr(approvals_api, "fan_out_approval", must_not_run)
    with pytest.raises(HTTPException) as raised:
        await approvals_api.review_artifact("a1", ReviewRequest(decision="approve"), SimpleNamespace(state=SimpleNamespace(current_user=SimpleNamespace(id="u1"))))
    assert raised.value.status_code == 409 and calls == ["a1"]


async def test_revert_reads_the_change_set_for_update(monkeypatch):
    calls: list = []

    async def locked_read(change_set_id):
        calls.append(change_set_id)
        return SimpleNamespace(id=change_set_id, status="reverted", reverted_by_change_set_id="cs2")

    monkeypatch.setattr(approvals_api.ChangeSet, "get_for_update", staticmethod(locked_read))
    with pytest.raises(HTTPException) as raised:
        await approvals_api.revert_change_set("cs1")
    assert raised.value.status_code == 409 and calls == ["cs1"]


##############################
# BUG-032: one apply per site
##############################


async def test_change_set_apply_is_deferred_under_the_site_lock(monkeypatch):
    created: list = []

    async def fake_create(**kwargs):
        return SimpleNamespace(id="cs1")

    async def fake_create_task(**kwargs):
        created.append(kwargs)

    monkeypatch.setattr(approval_flow.ChangeSet, "create", staticmethod(fake_create))
    monkeypatch.setattr(approval_flow, "create_task", fake_create_task)
    await approval_flow.create_and_dispatch_change_set("a1", SimpleNamespace(id="site-9", platform="wordpress"), "b1", [])
    assert created[0]["name"] == "change_set_apply" and created[0]["lock"] == "site:site-9"


async def test_create_task_forwards_the_lock_into_the_defer_sql(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(Database, "get_session", lambda self: session)
    await task_queue.create_task(brand_id="b1", name="change_set_apply", queue="changes", lock="site:site-9")
    statement, params = session.executed[0]
    assert params["lock"] == "site:site-9" and ":lock" in str(statement)
    await task_queue.create_task(brand_id="b1", name="site_audit")
    assert session.executed[1][1]["lock"] is None


##############################
# BUG-033: attempt_count survives the failure rollback
##############################


async def test_attempt_count_increments_per_attempt_even_when_attempts_fail(monkeypatch):
    task = Task(id="t1", brand_id="b1", name="change_set_apply", attempt_count=0)
    session = FakeSession(task)
    monkeypatch.setattr(Database, "create_session", lambda self: session)
    for _ in range(3):
        with pytest.raises(CmsWriteError):
            async with task_queue.task_lifecycle("t1"):
                raise CmsWriteError("site down")
    assert task.attempt_count == 3 and task.status == STATUS_FAILED
