"""BUG-050 — brief / report / plan work facts state what actually happened:
drafts are not sends, site changes are counted net of undos, and GBP items
park as "waiting to be posted by hand" while GBP isn't connected. Pure
(monkeypatched collaborators, no DB)."""

import json
from datetime import date, datetime
from types import SimpleNamespace

import worker.tasks.briefs as briefs
import worker.tasks.reports as reports
from worker.tasks.briefs import CHANGE_LIVE, CHANGE_UNDO, CHANGE_UNDONE, change_set_outcome, describe_work

WEEK_START = datetime(2026, 10, 5)


def _change_set(id: str, applied_at: datetime, status: str = "applied", reverted_by: str | None = None):
    return SimpleNamespace(id=id, status=status, reverted_by_change_set_id=reverted_by, applied_at=applied_at, operations=[{"op": "set_field", "field": "title"}])


# R020's week: one apply + its revert, one review-ask template drafted, GBP posts drafted + approved, GBP not connected
R020_COUNTS = {"change_set_apply": 2, "technical_fix": 2, "review_asks": 1, "gbp_management": 1, "gbp_apply": 1, "site_audit": 1}
R020_SETS = [_change_set("undo", datetime(2026, 10, 7)), _change_set("orig", datetime(2026, 10, 6), status="reverted", reverted_by="undo")]


def test_change_set_outcome_tells_undo_undone_and_live_apart():
    revert_set_ids = {"undo"}
    assert change_set_outcome(R020_SETS[0], revert_set_ids) == CHANGE_UNDO
    assert change_set_outcome(R020_SETS[1], revert_set_ids) == CHANGE_UNDONE
    assert change_set_outcome(_change_set("live", datetime(2026, 10, 6)), revert_set_ids) == CHANGE_LIVE


def test_an_apply_and_its_revert_are_net_zero():
    facts = describe_work(R020_COUNTS, R020_SETS, WEEK_START, None, gbp_connected=False)
    assert facts["site_changes"] == {"changes_live_on_site_now": 0, "changes_applied_then_undone": 1, "undos_of_earlier_changes": 1}
    assert "change_set_apply" not in json.dumps(facts)  # the raw run count is never offered


def test_drafts_are_never_worded_as_sent_and_parked_gbp_waits_for_hand_posting():
    facts = describe_work(R020_COUNTS, R020_SETS, WEEK_START, None, gbp_connected=False)
    labels = {entry["what_happened"]: entry["times"] for entry in facts["activity"]}
    review = next(label for label in labels if label.startswith("review-request"))
    assert "no review request has been sent" in review and labels[review] == 1
    assert all("gbp" not in label for label in labels)
    gbp = facts["google_business_profile"]
    assert gbp["connected"] is False and "not being managed live" in gbp["status"]
    assert gbp["approved_items_waiting_to_be_posted_by_hand"] == 1 and gbp["post_batches_drafted"] == 1
    assert "how_to_read" in facts


def test_connected_gbp_reports_posts_and_no_gbp_work_omits_the_section():
    assert describe_work({"gbp_apply": 2}, [], WEEK_START, None, gbp_connected=True)["google_business_profile"] == {"connected": True, "post_batches_drafted": 0, "approved_items_posted_to_google": 2}
    assert "google_business_profile" not in describe_work({"site_audit": 1}, [], WEEK_START, None, gbp_connected=False)


def test_window_bounds_change_sets_and_unknown_or_client_tasks_get_plain_labels():
    sets = [_change_set("before", datetime(2026, 10, 1)), _change_set("in", datetime(2026, 10, 8)), _change_set("after", datetime(2026, 10, 12))]
    facts = describe_work({"upload_photos": 1, "brand_new_task": 3}, sets, WEEK_START, datetime(2026, 10, 12), gbp_connected=False)
    assert facts["site_changes"]["changes_live_on_site_now"] == 1
    assert {entry["what_happened"] for entry in facts["activity"]} == {"completed by the client: upload photos", "brand new task runs"}


async def test_work_facts_asks_about_gbp_only_when_there_is_gbp_work(monkeypatch):
    asked: list[str] = []

    async def connected(brand_id):
        asked.append(brand_id)
        return [SimpleNamespace(provider="wordpress")]

    monkeypatch.setattr(briefs.Connection, "connected_for_brand", connected)
    assert "google_business_profile" not in await briefs.work_facts("b1", {"site_audit": 1}, [], since=WEEK_START)
    assert asked == []
    facts = await briefs.work_facts("b1", {"gbp_apply": 1}, [], since=WEEK_START)
    assert asked == ["b1"] and facts["google_business_profile"]["connected"] is False


async def test_monthly_report_lists_only_changes_still_live(monkeypatch):
    live = _change_set("live", datetime(2026, 8, 3))
    sets = [_change_set("undo", datetime(2026, 8, 21)), _change_set("orig", datetime(2026, 8, 20), status="reverted", reverted_by="undo"), live]

    async def empty(*args, **kwargs):
        return []

    async def change_sets(brand_id):
        return sets

    async def task_counts(brand_id, since, until=None):
        return {"change_set_apply": 3, "content_draft": 1}

    async def totals(brand_id, start, end):
        return {}

    monkeypatch.setattr(reports.VisibilityRun, "latest", empty)
    monkeypatch.setattr(reports.Finding, "open_as_of", empty)
    monkeypatch.setattr(reports.Finding, "resolved_since", empty)
    monkeypatch.setattr(reports.ChangeSet, "for_brand", change_sets)
    monkeypatch.setattr(reports, "completed_task_counts", task_counts)
    monkeypatch.setattr(reports.MetricPoint, "totals_for_period", totals)
    monkeypatch.setattr(reports, "overdue_client_tasks", empty)

    facts = await reports._assemble_report_facts(SimpleNamespace(id="b1"), date(2026, 8, 1))
    assert [change["applied_at"] for change in facts["site_changes_live_on_site_now"]] == ["2026-08-03"]
    assert facts["work_this_month"]["site_changes"] == {"changes_live_on_site_now": 1, "changes_applied_then_undone": 1, "undos_of_earlier_changes": 1}
    assert facts["work_this_month"]["activity"] == [{"what_happened": "articles drafted (not published)", "times": 1}]
    assert "work_shipped" not in facts and "site_changes_applied" not in facts


def test_prompts_tell_the_am_how_to_read_the_work_facts():
    brand = SimpleNamespace(name="B", website_url="https://b.com")
    for prompt in (briefs._brief_prompt(brand, {}), reports._report_prompt(brand, date(2026, 8, 1), {})):
        assert "a draft was not sent, posted or published" in prompt
