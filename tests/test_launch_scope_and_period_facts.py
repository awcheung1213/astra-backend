"""Pure (no DB, no client fixture): collaborators are monkeypatched, or the
session is a statement-capturing fake; the real code paths run.

BUG-034 cron work + weekly collection only for LAUNCHED brands;
BUG-036 monthly report facts bounded to [period, period_end);
BUG-035 negative-review alert is urgent; BUG-019 wizard re-entry validates
and applies; BUG-020 one homepage per audit; BUG-021 Undo has its own Pulse
wording; BUG-025 first-fill batches link the sign-off task."""

import contextlib
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

import api.onboarding as onboarding
import worker.tasks.changes as changes
import worker.tasks.collection as collection
import worker.tasks.reports as reports
import worker.tasks.reputation as reputation
import worker.tasks.schedules as schedules
import worker.tasks.site_audit as site_audit
from objects.batch import Batch
from objects.brand import Brand
from objects.finding import Finding
from objects.notification import URGENCY_URGENT
from objects.site import Site
from schemas.onboarding_schemas import SignupRequest
from utils.database import Database
from worker.tasks.briefs import completed_task_counts


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self.rows)

    def first(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


class CaptureSession:
    """Records every statement; answers with canned rows."""

    def __init__(self, rows=()):
        self.statements: list = []
        self.added: list = []
        self.rows = list(rows)

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(self.rows)

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        return None

    async def refresh(self, row):
        return None

    async def commit(self):
        return None


def _sql(statement) -> str:
    return " ".join(str(statement.compile(dialect=postgresql.dialect())).split())


@pytest.fixture
def session(monkeypatch):
    fake = CaptureSession()
    monkeypatch.setattr(Database, "get_session", lambda self: fake)  # ReplicaDatabase inherits it
    return fake


@contextlib.asynccontextmanager
async def _fake_lifecycle(task_id, payload=None):
    yield SimpleNamespace(id=task_id, payload=payload or {})


##############################
# BUG-034 — cron + collection fan out to launched brands only
##############################


async def test_launched_brands_query_filters_on_onboarded_at(session):
    await Brand.launched()
    assert "brands.onboarded_at IS NOT NULL" in _sql(session.statements[0])


async def test_weekly_audit_sites_are_launched_brands_only(session):
    await Site.connected()
    sql = _sql(session.statements[0])
    assert "JOIN brands ON brands.id = sites.brand_id" in sql and "brands.onboarded_at IS NOT NULL" in sql and "sites.status =" in sql


async def test_per_brand_cron_fans_out_to_launched_brands(monkeypatch, session):
    enqueued: list[str] = []

    async def launched():
        return [SimpleNamespace(id="launched-1")]

    async def forbidden_all():
        raise AssertionError("cron fan-out must not use Brand.all() (unlaunched brands included)")

    async def fake_create_task(brand_id, name, queue, payload=None):
        enqueued.append(brand_id)

    @contextlib.asynccontextmanager
    async def no_connection(self):
        yield session

    monkeypatch.setattr(schedules.Brand, "launched", launched)
    monkeypatch.setattr(schedules.Brand, "all", forbidden_all)
    monkeypatch.setattr(schedules, "create_task", fake_create_task)
    monkeypatch.setattr(Database, "connection", no_connection)
    await schedules.schedule_link_prospecting.func(timestamp=0)
    assert enqueued == ["launched-1"]


@pytest.fixture
def submit_harness(monkeypatch, session):
    """run_submit_prompt_execution_batch with every collaborator faked: each
    brand has one active prompt (own text) in market m1; nothing is fresh."""
    state: dict = {"batches": [], "requests": []}
    brands = {"launched": SimpleNamespace(id="launched", footprint="local"), "unlaunched": SimpleNamespace(id="unlaunched", footprint="local")}

    async def no_cells():
        return {}

    async def launched():
        return [brands["launched"]]

    async def brand_get(brand_id):
        return brands[brand_id]

    async def prompts_by_brand(brand_id):
        return [SimpleNamespace(id=f"p-{brand_id}", text=f"question for {brand_id}", is_active=True)]

    async def markets_by_brand(brand_id, brand_footprint):
        return [SimpleNamespace(id="m1")]

    async def nothing_fresh(**kwargs):
        return []

    async def all_markets():
        return [SimpleNamespace(id="m1", city="Mesa", state="AZ", zip=None, source="local")]

    class FakeEngine:
        async def submit_batch(self, query_requests):
            state["requests"].extend(query_requests)
            return SimpleNamespace(provider="claude", batch_id="pb1", status="in_progress")

    async def batch_create(**fields):
        state["batches"].append(fields)

    monkeypatch.setattr(collection.settings, "ACTIVE_ENGINES", ["claude"])
    monkeypatch.setattr(collection, "_in_process_cells", no_cells)
    monkeypatch.setattr(collection.Brand, "launched", launched)
    monkeypatch.setattr(collection.Brand, "get", brand_get)
    monkeypatch.setattr(collection.Prompt, "by_brand", prompts_by_brand)
    monkeypatch.setattr(collection.Market, "by_brand", markets_by_brand)
    monkeypatch.setattr(collection.Market, "all", all_markets)
    monkeypatch.setattr(collection.PromptResult, "within_freshness_window", nothing_fresh)
    monkeypatch.setattr(collection.QueryResponse, "within_freshness_window", nothing_fresh)
    monkeypatch.setattr(collection, "initialize_adapter", lambda adapter: FakeEngine())
    monkeypatch.setattr(collection.Batch, "create", batch_create)
    return state


async def test_weekly_collection_skips_unlaunched_brands(submit_harness):
    await collection.run_submit_prompt_execution_batch()
    assert [request.prompt for request in submit_harness["requests"]] == ["question for launched"]
    assert submit_harness["batches"][0]["task_id"] is None


##############################
# BUG-025 — a sign-off's first fill links its batch to the task
##############################


async def test_signoff_first_fill_links_batch_to_task_and_includes_signing_brand(submit_harness):
    await collection.run_submit_prompt_execution_batch(task_id="t-signoff", brand_id="unlaunched")
    assert sorted(request.prompt for request in submit_harness["requests"]) == ["question for launched", "question for unlaunched"]
    assert submit_harness["batches"][0]["task_id"] == "t-signoff"


async def test_submit_task_passes_its_task_id_through(monkeypatch):
    runs: list[dict] = []

    async def fake_run(**kwargs):
        runs.append(kwargs)

    monkeypatch.setattr(collection, "task_lifecycle", _fake_lifecycle)
    monkeypatch.setattr(collection, "run_submit_prompt_execution_batch", fake_run)
    await collection.submit_prompt_execution_batch.func(task_id="t1", brand_id="b1")
    assert runs == [{"task_id": "t1", "brand_id": "b1"}]


async def test_batch_create_stores_task_id(session):
    await Batch.create(provider="claude", batch_id="pb1", brand_id=None, status="in_progress", request_metadata={}, task_id="t1")
    assert session.added[0].task_id == "t1"


##############################
# BUG-036 — monthly report facts are bounded to the period
##############################


async def test_completed_task_counts_has_an_upper_bound(session):
    await completed_task_counts("b1", since=datetime(2026, 8, 1), until=datetime(2026, 9, 1))
    sql = _sql(session.statements[0])
    assert "tasks.finished_at >= " in sql and "tasks.finished_at < " in sql


async def test_resolved_findings_have_an_upper_bound(session):
    await Finding.resolved_since("b1", since=datetime(2026, 8, 1), until=datetime(2026, 9, 1))
    sql = _sql(session.statements[0])
    assert "findings.resolved_at >= " in sql and "findings.resolved_at < " in sql


async def test_open_findings_as_of_period_end(session):
    await Finding.open_as_of("b1", at=datetime(2026, 9, 1))
    sql = _sql(session.statements[0])
    assert "findings.created_at < " in sql and "findings.resolved_at >= " in sql


async def test_report_facts_bound_every_period_count(monkeypatch):
    calls: dict = {}
    august, september = datetime(2026, 8, 1), datetime(2026, 9, 1)

    async def latest_runs(brand_id, limit):
        return [SimpleNamespace(week_of=date(2026, 8, 9), overall_score=0.4, detection_rate=0.5, share_of_voice=0.2), SimpleNamespace(week_of=date(2026, 9, 6), overall_score=0.9, detection_rate=0.9, share_of_voice=0.9)]

    async def open_as_of(brand_id, at):
        calls["open_as_of"] = at
        return []

    async def forbidden_open_now(brand_id):
        raise AssertionError("open findings must be as of period end, not now")

    async def resolved(brand_id, since, until=None):
        calls["resolved"] = (since, until)
        return []

    async def change_sets(brand_id):
        return [
            SimpleNamespace(id="cs1", status="applied", reverted_by_change_set_id=None, applied_at=datetime(2026, 8, 20), operations=[{"op": "set_field", "field": "title"}]),
            SimpleNamespace(id="cs2", status="applied", reverted_by_change_set_id=None, applied_at=datetime(2026, 9, 2), operations=[{"op": "set_field", "field": "title"}]),
        ]

    async def task_counts(brand_id, since, until=None):
        calls["tasks"] = (since, until)
        return {}

    async def totals(brand_id, start, end):
        calls["metrics"] = (start, end)
        return {}

    async def no_overdue(brand_id):
        return []

    monkeypatch.setattr(reports.VisibilityRun, "latest", latest_runs)
    monkeypatch.setattr(reports.Finding, "open_as_of", open_as_of)
    monkeypatch.setattr(reports.Finding, "open_for_brand", forbidden_open_now)
    monkeypatch.setattr(reports.Finding, "resolved_since", resolved)
    monkeypatch.setattr(reports.ChangeSet, "for_brand", change_sets)
    monkeypatch.setattr(reports, "completed_task_counts", task_counts)
    monkeypatch.setattr(reports.MetricPoint, "totals_for_period", totals)
    monkeypatch.setattr(reports, "overdue_client_tasks", no_overdue)

    facts = await reports._assemble_report_facts(SimpleNamespace(id="b1"), date(2026, 8, 1))
    assert calls["tasks"] == (august, september)
    assert calls["resolved"] == (august, september)
    assert calls["open_as_of"] == september
    assert calls["metrics"] == (date(2026, 8, 1), date(2026, 9, 1))
    assert [score["week_of"] for score in facts["visibility"]["weekly_scores"]] == ["2026-08-09"]
    assert [change["applied_at"] for change in facts["site_changes_live_on_site_now"]] == ["2026-08-20"]


##############################
# BUG-035 — a negative-review alert is urgent
##############################


async def test_negative_review_alert_is_urgent(monkeypatch):
    notifications: list[dict] = []

    @contextlib.asynccontextmanager
    async def lifecycle(task_id):
        yield SimpleNamespace(id=task_id, payload={"review_id": "r1"})

    async def review_get(review_id):
        return SimpleNamespace(id="r1", reply_artifact_id=None, rating=2, platform="google", author="A", text="meh", external_id="x")

    async def brand_get(brand_id):
        return SimpleNamespace(id="b1", organization_id="o1", name="Ace", website_url="https://ace.example", description="Plumbing")

    class FakeAgent:
        async def run_role(self, *args, **kwargs):
            return '{"reply": "Sorry to hear that."}'

    async def artifact_create(**fields):
        return SimpleNamespace(id="a1", **fields)

    async def link_reply(review_id, artifact_id):
        return None

    async def notification_create(**fields):
        notifications.append(fields)

    monkeypatch.setattr(reputation, "task_lifecycle", lifecycle)
    monkeypatch.setattr(reputation.Review, "get", review_get)
    monkeypatch.setattr(reputation.Review, "link_reply", link_reply)
    monkeypatch.setattr(reputation.Brand, "get", brand_get)
    monkeypatch.setattr(reputation, "ClaudeAgent", FakeAgent)
    monkeypatch.setattr(reputation.Artifact, "create", artifact_create)
    monkeypatch.setattr(reputation.Notification, "create", notification_create)
    await reputation.review_response.func("t1", "b1")
    assert notifications[0]["urgency"] == URGENCY_URGENT


##############################
# BUG-019 — wizard re-entry validates and applies
##############################


@pytest.fixture
def reentry_harness(monkeypatch):
    state: dict = {"brand_updates": [], "org_updates": [], "markets": None, "onboarded_at": None}

    async def by_org(organization_id):
        return [SimpleNamespace(id="b1", onboarded_at=state["onboarded_at"])]

    async def brand_update(id, **fields):
        state["brand_updates"].append(fields)
        return SimpleNamespace(id=id, **fields)

    async def org_update(id, name=None, plan=None):
        state["org_updates"].append(name)

    async def get_or_create_city(city, state_code):
        return SimpleNamespace(id=f"{city.lower()}-{state_code}")

    async def replace_user_markets(brand_id, market_ids):
        state["markets"] = market_ids

    monkeypatch.setattr(onboarding, "current_user", lambda request: SimpleNamespace(id="u1", organization_id="o1"))
    monkeypatch.setattr(onboarding.Brand, "by_organization_id", by_org)
    monkeypatch.setattr(onboarding.Brand, "update", brand_update)
    monkeypatch.setattr(onboarding.Brand, "replace_user_markets", replace_user_markets)
    monkeypatch.setattr(onboarding.Organization, "update", org_update)
    monkeypatch.setattr(onboarding.Market, "get_or_create_city", get_or_create_city)
    return state


def _signup_request(**overrides) -> SignupRequest:
    return SignupRequest(**{"organization_name": "", "brand_name": "Cedar Dental", "website_url": "cedar.example", "footprint": "local", "cities": ["Phoenix, AZ", "mesa, az"], **overrides})


async def test_reentry_rejects_an_invalid_city_and_changes_nothing(reentry_harness):
    with pytest.raises(HTTPException) as error:
        await onboarding.signup(_signup_request(cities=["Phoenix, AZ", "Foo, ZZ"]), SimpleNamespace())
    assert error.value.status_code == 422 and "Foo, ZZ" in error.value.detail
    assert reentry_harness["brand_updates"] == [] and reentry_harness["markets"] is None


async def test_reentry_applies_name_site_and_coverage(reentry_harness):
    response = await onboarding.signup(_signup_request(), SimpleNamespace())
    assert (response.organization_id, response.brand_id) == ("o1", "b1")
    assert reentry_harness["brand_updates"] == [{"name": "Cedar Dental", "website_url": "https://cedar.example", "footprint": "local"}]
    assert reentry_harness["org_updates"] == ["Cedar Dental"]
    assert reentry_harness["markets"] == ["phoenix-AZ", "mesa-AZ"]


async def test_reentry_after_launch_still_409s(reentry_harness):
    reentry_harness["onboarded_at"] = datetime(2026, 9, 1)
    with pytest.raises(HTTPException) as error:
        await onboarding.signup(_signup_request(), SimpleNamespace())
    assert error.value.status_code == 409 and reentry_harness["brand_updates"] == []


##############################
# BUG-020 — one page, one spelling
##############################


def test_normalize_page_url():
    assert site_audit.normalize_page_url("https://Evergreenofficial.com") == "https://evergreenofficial.com/"
    assert site_audit.normalize_page_url("https://evergreenofficial.com/") == "https://evergreenofficial.com/"
    assert site_audit.normalize_page_url("https://evergreenofficial.com:443/#top") == "https://evergreenofficial.com/"
    assert site_audit.normalize_page_url("http://site.example:80/about") == "http://site.example/about"
    assert site_audit.normalize_page_url("http://site.example:8080/about/") == "http://site.example:8080/about/"
    assert site_audit.normalize_page_url("https://site.example/shop?page=2#x") == "https://site.example/shop?page=2"


def test_finding_key_treats_root_spellings_as_one_page():
    assert site_audit.finding_key("missing_schema", {"url": "https://site.example"}) == site_audit.finding_key("missing_schema", {"url": "https://site.example/"})
    assert site_audit.finding_key("missing_schema", {}) == ("missing_schema", "")


def test_existing_twin_findings_collapse_to_the_oldest():
    older = SimpleNamespace(id="f-old", kind="missing_schema", detail={"url": "https://site.example"}, created_at=datetime(2026, 9, 1))
    newer = SimpleNamespace(id="f-new", kind="missing_schema", detail={"url": "https://site.example/"}, created_at=datetime(2026, 9, 2))
    by_key, duplicates = site_audit.split_duplicate_open_findings([newer, older])
    assert [finding.id for finding in by_key.values()] == ["f-old"] and [finding.id for finding in duplicates] == ["f-new"]


async def test_crawl_audits_the_homepage_once(monkeypatch):
    fetched: list[str] = []
    home_html = '<html><head><title>Home</title></head><body><h1>Hi</h1><a href="/">Home</a><a href="https://site.example/#main">Top</a><a href="/about">About</a></body></html>'

    async def fake_fetch(client, url):
        fetched.append(url)
        if url.endswith("robots.txt") or "sitemap" in url:
            return None
        return SimpleNamespace(status_code=200, headers={"content-type": "text/html"}, text=home_html)

    @contextlib.asynccontextmanager
    async def fake_session(**kwargs):
        yield None

    monkeypatch.setattr(site_audit, "_fetch", fake_fetch)
    monkeypatch.setattr(site_audit, "site_session", fake_session)
    findings = await site_audit._crawl("https://site.example")
    pages = [url for url in fetched if not url.endswith("robots.txt") and "sitemap" not in url]
    assert pages == ["https://site.example/", "https://site.example/about"]
    schema_urls = [finding["detail"]["url"] for finding in findings if finding["kind"] == "missing_schema"]
    assert schema_urls == ["https://site.example/", "https://site.example/about"]


##############################
# BUG-021 — Undo has its own Pulse wording
##############################


def test_revert_notification_wording():
    # the Pulse line names what changed and where (marco-review T4); an Undo never reads like a new change
    original = SimpleNamespace(id="cs-1", status="reverted", reverted_by_change_set_id="cs-2", operations=[{"op": "set_field", "field": "title", "target": "https://x.com/about/"}])
    undo = SimpleNamespace(id="cs-2", status="applied", reverted_by_change_set_id=None, operations=[{"op": "set_field", "field": "title", "target": "https://x.com/about/"}])
    assert changes.change_notification_text(undo, None, original) == ("Undid the change to your /about/ page", "Your /about/ page is back the way it was. Tap to see what changed.")
    applied = SimpleNamespace(id="cs-3", status="applied", reverted_by_change_set_id=None, operations=[{"op": "set_field", "field": "title", "target": "https://x.com/", "value": "New", "previous_value": "Old"}])
    assert changes.change_notification_text(applied, None) == ("Set the title Google shows for your homepage", "Live now on your site. Tap to see what changed.")


async def test_a_revert_set_is_found_by_the_originals_link(session):
    session.rows = [SimpleNamespace(id="original-id")]
    assert (await changes._reverted_original("revert-id")).id == "original-id"
    assert "change_sets.reverted_by_change_set_id = " in _sql(session.statements[0])
    session.rows = []
    assert await changes._reverted_original("plain-id") is None


async def test_apply_of_a_revert_set_posts_the_undo_wording(monkeypatch, session):
    notifications: list[dict] = []
    change_set = SimpleNamespace(id="cs-revert", artifact_id="a1", status="pending", site_id="s1", reverted_by_change_set_id=None, operations=[{"id": "op1", "op": "set_field", "field": "title", "target": "https://x.com/", "completed_at": None}])
    original = SimpleNamespace(id="cs-original", status="reverted", reverted_by_change_set_id="cs-revert", operations=[{"id": "op1", "op": "set_field", "field": "title", "target": "https://x.com/"}])
    synced: list[str] = []

    @contextlib.asynccontextmanager
    async def lifecycle(task_id):
        yield SimpleNamespace(id=task_id, payload={"change_set_id": "cs-revert"})

    async def change_set_get(id):
        return change_set

    async def site_get(id):
        return SimpleNamespace(id="s1", platform="wordpress")

    async def update_status(id, status):
        change_set.status = status

    async def mark_complete(id, operation_id, apply_result):
        change_set.operations = [{**change_set.operations[0], "completed_at": "now"}]

    async def apply_operation(site, operation, checkpoint):
        return {"previous_value": "old"}

    async def brand_get(brand_id):
        return SimpleNamespace(id="b1", organization_id="o1")

    async def reverted_original(change_set_id):
        return original

    async def sync_findings(artifact_id):
        synced.append(artifact_id)
        return []

    async def session_get(model, id):
        return None

    async def notification_create(**fields):
        notifications.append(fields)

    session.get = session_get
    monkeypatch.setattr(changes, "task_lifecycle", lifecycle)
    monkeypatch.setattr(changes.ChangeSet, "get", change_set_get)
    monkeypatch.setattr(changes.ChangeSet, "update_status", update_status)
    monkeypatch.setattr(changes.ChangeSet, "mark_operation_complete", mark_complete)
    monkeypatch.setattr(changes.Site, "get", site_get)
    monkeypatch.setattr(changes, "_apply_operation", apply_operation)
    monkeypatch.setattr(changes.Brand, "get", brand_get)
    monkeypatch.setattr(changes, "_reverted_original", reverted_original)
    monkeypatch.setattr(changes, "sync_findings_with_fix", sync_findings)
    monkeypatch.setattr(changes.Notification, "create", notification_create)
    await changes.change_set_apply.func("t1", "b1")
    assert synced == ["a1"]  # the finding lifecycle rides the same apply
    assert notifications[0]["title"] == "Undid the change to your homepage" and notifications[0]["link_ref"] == "change_sets/cs-revert"
