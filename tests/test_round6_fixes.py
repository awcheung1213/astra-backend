"""Marco review round 6 — the P0 dead ends, pure functions only (no DB, no network):
plan questions answered on the card, the honest review-ask text, Google posts
he posts himself, and Google descriptions that actually reach Google (Yoast
field, helper-plugin bridge, live re-read)."""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from adapters.cms.common import SeoBridgeNeeded
from adapters.cms.wordpress_adapter import SEO_BRIDGE_PLUGIN, SEO_YOAST, YOAST_DESCRIPTION_META, WordPressAdapter, seo_plugin_from_namespaces
from objects.artifact import STATUS_APPROVED, STATUS_PENDING_REVIEW, STATUS_REJECTED
from utils import receipt_facts, receipts, study_asks
from worker.tasks import changes
from worker.tasks.reputation import review_ask_is_current, review_ask_text, review_search_link

NOW = datetime(2026, 10, 14, 12)

# ---------- plan questions (#1, #2) ----------


def test_baseline_step_asks_for_todays_numbers():
    asks = study_asks.default_asks("Set the measurement baseline")
    assert [ask["key"] for ask in asks] == [study_asks.KEY_MONTHLY_REVENUE, study_asks.KEY_MONTHLY_CUSTOMERS]
    assert asks[0]["kind"] == study_asks.KIND_NUMBER and not asks[0]["optional"]


def test_proof_step_asks_for_customers_to_feature_and_anything_else_one_question():
    assert study_asks.default_asks("Proof that the work actually happens")[0]["key"] == "featured_customers"
    generic = study_asks.default_asks("Comparison pages")
    assert len(generic) == 1 and "Comparison pages" in generic[0]["question"]


def test_strategist_asks_are_cleaned_and_capped():
    raw = [{"question": "Jobs booked last month?", "kind": "number", "unit": "jobs"}, {"question": "  "}, {"key": "x", "question": "A", "kind": "weird"}, {"question": "B"}, {"question": "C"}]
    asks = study_asks.normalize_asks(raw)
    assert len(asks) == study_asks.MAX_ASKS
    assert asks[0]["key"] == "jobs_booked_last_month" and asks[0]["kind"] == "number"
    assert asks[1]["kind"] == study_asks.KIND_TEXT  # unknown kind -> text


def test_workstream_asks_prefer_the_strategists_then_defaults_then_none():
    assert study_asks.workstream_asks({"name": "Set the measurement baseline", "needs_client_data": True})[0]["key"] == study_asks.KEY_MONTHLY_REVENUE
    assert study_asks.workstream_asks({"name": "x", "client_asks": [{"question": "How many trucks?", "kind": "number"}]})[0]["question"] == "How many trucks?"
    assert study_asks.workstream_asks({"name": "Close the site findings"}) == []


def test_tasks_born_before_asks_get_defaults():
    assert study_asks.asks_for({"workstream": "Set the measurement baseline"})[0]["key"] == study_asks.KEY_MONTHLY_REVENUE


def test_answers_parse_money_and_require_the_required_one():
    asks = study_asks.default_asks("Set the measurement baseline")
    answers, problems = study_asks.clean_answers(asks, {"monthly_revenue": "$12,500", "monthly_customers": "40"})
    assert problems == [] and answers == {"monthly_revenue": 12500.0, "monthly_customers": 40.0}
    assert study_asks.clean_answers(asks, {"monthly_customers": "40"})[1]  # revenue is required
    assert "needs a number" in study_asks.clean_answers(asks, {"monthly_revenue": "lots"})[1][0]
    assert study_asks.parse_number("12.5k") == 12500 and study_asks.parse_number("1.2m") == 1_200_000


def test_answered_lines_read_back_in_ask_order():
    payload = {"workstream": "Set the measurement baseline", "answers": {"monthly_customers": 40.0, "monthly_revenue": 12500.0}}
    lines = study_asks.answered_lines(payload)
    assert [line["value"] for line in lines] == [12500.0, 40.0]


# ---------- review asks (#4) ----------


def test_review_ask_text_is_short_named_and_linked():
    link = review_search_link("Reyes Plumbing & Drain", "Phoenix AZ")
    assert link == "https://www.google.com/search?q=Reyes+Plumbing+%26+Drain+Phoenix+AZ"
    text = review_ask_text("Reyes Plumbing & Drain", link)
    assert "Reyes Plumbing & Drain" in text and link in text and "{" not in text


def test_review_ask_quiets_for_two_weeks_after_he_sends_it():
    assert review_ask_is_current(STATUS_PENDING_REVIEW, None, NOW)
    assert review_ask_is_current(STATUS_APPROVED, NOW - timedelta(days=3), NOW)
    assert not review_ask_is_current(STATUS_APPROVED, NOW - timedelta(days=15), NOW)
    assert not review_ask_is_current(STATUS_REJECTED, NOW - timedelta(days=1), NOW)  # "Not now" comes back next week


# ---------- decisions read honestly (#5, #9) ----------


def test_owner_posted_google_posts_and_sent_review_text_read_as_his_doing():
    posts = SimpleNamespace(type="gbp_touches", status=STATUS_APPROVED, content={"posts": ["a"], "posted_by": "owner"})
    assert receipt_facts.decision(posts) == receipt_facts.DECISION_POSTED_YOURSELF
    assert receipt_facts.decision(SimpleNamespace(type="gbp_touches", status=STATUS_APPROVED, content={"posts": ["a"]})) == receipt_facts.DECISION_APPROVED
    assert receipt_facts.decision(SimpleNamespace(type="review_ask", status=STATUS_APPROVED, content={})) == receipt_facts.DECISION_SENT_YOURSELF


# ---------- Google descriptions (#3) ----------


def test_yoast_detected_from_rest_namespaces():
    assert seo_plugin_from_namespaces(["wp/v2", "yoast/v1"]) == SEO_YOAST
    assert seo_plugin_from_namespaces(["wp/v2"]) is None


def _adapter(seo: str | None, calls: list):
    adapter = WordPressAdapter.__new__(WordPressAdapter)
    adapter.base_url = "https://reyes.com"
    adapter._seo_plugin_name = seo

    async def request(client, method, path, json_body=None):
        calls.append((method, path, json_body))
        return {}

    adapter._request = request  # type: ignore[method-assign]
    return adapter


def test_yoast_description_without_the_bridge_raises_before_writing():
    calls: list = []
    adapter = _adapter(SEO_YOAST, calls)
    page = {"id": 7, "meta": {}, "excerpt": {"raw": "old"}}
    with pytest.raises(SeoBridgeNeeded):
        asyncio.run(adapter._set_field(None, "pages", page, "meta_description", "New description"))
    assert calls == []


def test_yoast_description_writes_yoasts_field_and_records_the_old_one():
    calls: list = []
    adapter = _adapter(SEO_YOAST, calls)
    page = {"id": 7, "meta": {YOAST_DESCRIPTION_META: "Old"}, "excerpt": {"raw": "excerpt"}}
    result = asyncio.run(adapter._set_field(None, "pages", page, "meta_description", "New description"))
    assert result == {"previous_value": "Old"}
    assert calls == [("POST", "/wp-json/wp/v2/pages/7", {"meta": {YOAST_DESCRIPTION_META: "New description"}})]


def test_without_yoast_the_description_stays_on_the_excerpt():
    calls: list = []
    adapter = _adapter(None, calls)
    asyncio.run(adapter._set_field(None, "pages", {"id": 7, "excerpt": {"raw": "old"}}, "meta_description", "New"))
    assert calls == [("POST", "/wp-json/wp/v2/pages/7", {"excerpt": "New"})]


def test_bridge_proposal_converts_to_an_install_and_undoes_to_a_deactivate():
    content = changes.seo_bridge_proposal("https://reyes.com", ["cs-1"])
    assert content["waiting_change_set_ids"] == ["cs-1"] and content["display"]["title"].startswith("Add a free helper plugin")
    operations = changes.operations_for_artifact(SimpleNamespace(type="change_proposal", content=content))
    assert operations is not None and operations[0]["op"] == "install_plugin" and operations[0]["value"] == {"slug": SEO_BRIDGE_PLUGIN}
    applied = SimpleNamespace(operations=[{**operations[0], "completed_at": "x", "created_ref": "seo-fields-api-support/plugin", "previous_value": None}])
    assert changes.invert_operations(applied) == [{"id": "op-0", "op": "deactivate_plugin", "target": "seo-fields-api-support/plugin", "value": None, "previous_value": None, "completed_at": None}]
    already_active = SimpleNamespace(operations=[{**operations[0], "completed_at": "x", "created_ref": "p/p", "previous_value": "active"}])
    assert changes.invert_operations(already_active) is None  # it was his already: nothing of ours to turn off


def test_plugin_receipts_read_in_plain_words():
    assert receipts.describe_operation({"op": "install_plugin", "value": {"slug": SEO_BRIDGE_PLUGIN}})["what"].startswith("Added a free helper plugin")
    assert receipts.past_tense("Turn off the helper plugin") == "Turned off the helper plugin"


def test_only_completed_description_writes_get_a_live_check():
    operations = [
        {"op": "set_field", "field": "meta_description", "target": "https://reyes.com/about/", "value": "About us", "completed_at": "x"},
        {"op": "set_field", "field": "content", "target": "https://reyes.com/", "value": "<p>", "completed_at": "x"},
        {"op": "set_field", "field": "meta_description", "target": "https://reyes.com/contact/", "value": "Call", "completed_at": None},
    ]
    assert changes.description_targets(operations) == [("https://reyes.com/about/", "About us")]


def test_live_page_description_is_read_whatever_the_attribute_order():
    assert changes.page_description('<meta name="description" content="Plumbers &amp; drains in Phoenix" />') == "Plumbers & drains in Phoenix"
    assert changes.page_description("<meta content='Fast fixes' name='description'>") == "Fast fixes"
    assert changes.page_description('<meta property="og:description" content="Social only" />') is None


def test_shows_description_needs_the_google_tag_not_the_social_one():
    social_only = '<meta property="og:description" content="Why Evergreen exists" />'
    assert not changes.shows_description(social_only, "Why Evergreen exists")
    assert changes.shows_description('<meta name="description" content="Why  Evergreen exists">', "Why Evergreen exists")
