"""Site traffic on Today (utils/traffic.py) + one-tap Google (api/connections.py
matchers): pure functions only — no database, no network."""

from datetime import date, timedelta

from api.connections import PROVIDER_GOOGLE, callback_result, granted_read_providers, match_ga4_property, match_gsc_property, measurement_ids_in_html
from utils import traffic
from worker.tasks.metrics import BACKFILL_DAYS, SYNC_WINDOW_DAYS, ai_session_points, sync_window_days

END = date(2026, 10, 12)
GSC = "https://www.googleapis.com/auth/webmasters.readonly"
GA4 = "https://www.googleapis.com/auth/analytics.readonly"


def _flat(per_day: float, days: int = 120, end: date = END) -> dict[date, float]:
    return {end - timedelta(days=offset): per_day for offset in range(days)}


def _kpi(daily: dict[date, float], source: str = traffic.SOURCE_GA4, joined_on: date | None = None, ai: dict[date, float] | None = None, by_assistant: dict[str, float] | None = None) -> dict:
    series = {traffic.SERIES_KEY[source]: daily, traffic.AI_SERIES_KEY: ai or {}}
    return traffic.traffic_kpi(source, traffic.STATE_READY, series, by_assistant or {}, joined_on)


# ---------- source + state ----------


def test_ga4_beats_gsc_and_an_unsynced_source_is_loading():
    assert traffic.pick_source({"gsc", "ga4"}, {"gsc", "ga4"}) == ("ga4", traffic.STATE_READY)
    assert traffic.pick_source({"gsc", "ga4"}, {"gsc"}) == ("ga4", traffic.STATE_LOADING)
    assert traffic.pick_source({"gsc"}, {"gsc"}) == ("gsc", traffic.STATE_READY)
    assert traffic.pick_source(set(), set()) == (None, traffic.STATE_NOT_CONNECTED)


def test_not_ready_returns_no_numbers():
    card = traffic.traffic_kpi("ga4", traffic.STATE_LOADING, {}, {}, None)
    assert card["state"] == traffic.STATE_LOADING and card["total"] is None and card["weeks"] == []


def test_synced_with_no_rows_is_zero_not_loading():
    card = _kpi({})
    assert card["state"] == traffic.STATE_READY and card["total"] == 0


# ---------- rolling windows ----------


def test_28_day_window_ends_at_the_newest_day_with_data():
    daily = _flat(10, end=END - timedelta(days=2))  # GSC: two days behind
    card = _kpi(daily, source=traffic.SOURCE_GSC)
    assert card["total"] == 280 and card["previous"] == 280
    assert card["updated_through"] == (END - timedelta(days=2)).isoformat()
    assert card["change"] == "steady" and card["change_pct"] == 0


def test_no_previous_window_without_enough_history():
    card = _kpi(_flat(10, days=40))
    assert card["total"] == 280 and card["previous"] is None and card["change"] is None


def test_twelve_weekly_bars_oldest_first_ending_at_the_newest_day():
    card = _kpi(_flat(10))
    assert len(card["weeks"]) == 12 and all(week["value"] == 70 for week in card["weeks"])
    assert card["weeks"][-1]["week_of"] == (END - timedelta(days=6)).isoformat()
    assert card["weeks"][0]["week_of"] < card["weeks"][-1]["week_of"]


def test_weeks_before_the_first_data_day_are_left_out():
    card = _kpi(_flat(10, days=21))
    assert len(card["weeks"]) == 3


# ---------- noise gate ----------


def test_change_needs_both_ten_percent_and_twenty_visits():
    assert traffic.change_word(1100, 1000) == "up"
    assert traffic.change_word(1050, 1000) == "steady"  # 5%: inside the dead zone
    assert traffic.change_word(30, 15) == "steady"  # +100% but only 15 visits
    assert traffic.change_word(800, 1000) == "down"
    assert traffic.change_word(25, 0) == "up"
    assert traffic.change_word(10, None) is None


# ---------- since you joined ----------


def test_since_joined_compares_the_last_28_days_to_the_4_weeks_before_joining():
    joined = END - timedelta(days=40)
    daily = {day: (12.0 if day >= joined else 10.0) for day in _flat(1)}
    card = _kpi(daily, joined_on=joined)
    assert card["since_joined_pct"] == 20
    assert card["joined_on"] == joined.isoformat()


def test_since_joined_waits_for_a_full_window():
    card = _kpi(_flat(10), joined_on=END - timedelta(days=10))
    assert card["since_joined_pct"] is None


def test_since_joined_needs_the_baseline_in_history():
    joined = END - timedelta(days=40)
    card = _kpi(_flat(10, days=50), joined_on=joined)  # history starts 10 days before joining
    assert card["since_joined_pct"] is None


# ---------- AI assistants ----------


def test_ai_visits_count_only_with_ga4_and_list_assistants_biggest_first():
    ai = {END - timedelta(days=offset): 1.0 for offset in range(14)}
    card = _kpi(_flat(10), ai=ai, by_assistant={"Perplexity": 3, "ChatGPT": 9, "Claude": 0.2})
    assert card["ai_visits"] == 14
    assert card["ai_by_assistant"] == [{"name": "ChatGPT", "visits": 9}, {"name": "Perplexity", "visits": 3}]
    gsc_card = _kpi(_flat(10), source=traffic.SOURCE_GSC, ai=ai)
    assert gsc_card["ai_visits"] is None and gsc_card["ai_by_assistant"] == []


def test_assistant_names_fold_hosts():
    assert traffic.assistant_for_source("chatgpt.com") == "ChatGPT"
    assert traffic.assistant_for_source("chat.openai.com") == "ChatGPT"
    assert traffic.assistant_for_source("www.perplexity.ai") == "Perplexity"
    assert traffic.assistant_for_source("google") is None


def test_ai_points_store_a_daily_total_and_a_separate_per_assistant_metric():
    day = date(2026, 10, 1)
    points = ai_session_points([{"day": day, "assistant": "ChatGPT", "sessions": 2.0}, {"day": day, "assistant": "Gemini", "sessions": 1.0}])
    totals = [point for point in points if point["metric"] == "ai_sessions"]
    split = [point for point in points if point["metric"] == traffic.AI_BY_ASSISTANT_METRIC]
    assert totals == [{"source": "ga4", "metric": "ai_sessions", "day": day, "value": 3.0}]
    assert {point["dimension"] for point in split} == {"ChatGPT", "Gemini"}
    assert all("dimension" not in point for point in totals)


# ---------- backfill window ----------


def test_backfill_window_is_clamped():
    assert sync_window_days(None) == SYNC_WINDOW_DAYS
    assert sync_window_days({"backfill_days": 90}) == BACKFILL_DAYS
    assert sync_window_days({"backfill_days": 5000}) == BACKFILL_DAYS
    assert sync_window_days({"backfill_days": "90"}) == SYNC_WINDOW_DAYS


# ---------- one-tap Google: matching ----------


def test_gsc_prefers_the_domain_property_then_https():
    assert match_gsc_property(["sc-domain:reyes.com", "https://reyes.com/"], "https://www.reyes.com") == "sc-domain:reyes.com"
    assert match_gsc_property(["http://reyes.com/", "https://reyes.com/"], "reyes.com") == "https://reyes.com/"
    assert match_gsc_property(["https://other.com/"], "reyes.com") is None
    assert match_gsc_property(["sc-domain:reyes.com", "sc-domain:shop.reyes.com"], "shop.reyes.com") is None  # both cover it: his pick


def test_ga4_homepage_tag_decides():
    streams = {"1": [{"measurement_id": "G-AAAA1111", "default_uri": "https://reyes.com"}], "2": [{"measurement_id": "G-BBBB2222", "default_uri": "https://reyes.com"}]}
    assert match_ga4_property(streams, "https://reyes.com", {"G-BBBB2222"}) == "2"
    assert match_ga4_property(streams, "https://reyes.com", set()) is None  # two on his domain, no tag: his pick


def test_ga4_stream_domain_then_only_property():
    streams = {"1": [{"measurement_id": "G-A1B2C3D4", "default_uri": "https://www.reyes.com"}], "2": [{"measurement_id": "G-X1", "default_uri": "https://cousin.com"}]}
    assert match_ga4_property(streams, "reyes.com", set()) == "1"
    assert match_ga4_property({"9": [{"measurement_id": "G-Z", "default_uri": ""}]}, "reyes.com", set()) == "9"
    assert match_ga4_property({"9": [{"measurement_id": "G-Z", "default_uri": "https://cousin.com"}]}, "reyes.com", set()) is None


def test_measurement_ids_found_in_html():
    html = "<script async src='https://www.googletagmanager.com/gtag/js?id=G-7QX2ABCD12'></script><script>gtag('config','G-7QX2ABCD12')</script>"
    assert measurement_ids_in_html(html) == {"G-7QX2ABCD12"}
    assert measurement_ids_in_html(None) == set()


def test_granted_scopes_decide_which_rows_land():
    assert granted_read_providers({"scope": f"{GSC} {GA4} openid"}, PROVIDER_GOOGLE) == ["gsc", "ga4"]
    assert granted_read_providers({"scope": GSC}, PROVIDER_GOOGLE) == ["gsc"]  # unticked Analytics
    assert granted_read_providers({}, PROVIDER_GOOGLE) == ["gsc", "ga4"]
    assert granted_read_providers({"scope": f"{GSC} {GA4}"}, "ga4") == ["ga4"]
    assert granted_read_providers({"scope": "openid"}, PROVIDER_GOOGLE) == []


def test_callback_result_words():
    assert callback_result({"gsc": "connected", "ga4": "connected"}) == "connected"
    assert callback_result({"gsc": "connected", "ga4": "no_properties"}) == "connected"  # no GA4 at all: clicks from Google
    assert callback_result({"gsc": "connected", "ga4": "pending"}) == "pick_property"
    assert callback_result({"gsc": "no_properties", "ga4": "no_properties"}) == "no_properties"  # the wrong Google account


def test_analytics_state_only_says_none_when_google_said_so():
    assert traffic.analytics_state({"ga4", "gsc"}, []) == traffic.ANALYTICS_CONNECTED
    assert traffic.analytics_state({"gsc"}, [{"ga4_on_account": False}]) == traffic.ANALYTICS_NONE_ON_ACCOUNT
    assert traffic.analytics_state({"gsc"}, [{}]) == traffic.ANALYTICS_NOT_CONNECTED
