"""AI answers screens — the owner aggregation rule and everything derived from
it (utils/answer_stats.py). Pure: SimpleNamespace rows, no DB, no network."""

from datetime import date, datetime, timedelta
from types import SimpleNamespace

from utils import answer_stats

NOW = datetime(2026, 10, 14, 12, 0)  # a Wednesday
ENGINES = ["chatgpt", "gemini", "claude"]
_ids = iter(range(1, 100000))


def _result(prompt_id="p1", engine="chatgpt", market_id="m1", mentioned=False, rank=None, created_at=NOW - timedelta(days=1), query_response_id=None):
    result_id = f"r{next(_ids)}"
    return SimpleNamespace(id=result_id, prompt_id=prompt_id, engine=engine, market_id=market_id, brand_mentioned=mentioned, rank=rank, created_at=created_at, query_response_id=query_response_id or f"qr-{result_id}", text=f"result text {result_id}")


def _prompt(prompt_id, is_active=True, tier=None):
    return SimpleNamespace(id=prompt_id, is_active=is_active, tier=tier or ("core" if is_active else "suggested"), category=None, last_checked_at=None, tier_changed_at=None)


def _mention(result, name, position=None):
    return SimpleNamespace(prompt_result_id=result.id, competitor_name=name, normalized_name=name.lower(), position=position)


##############################
# engine_stats — share, named threshold, rank average, not asked
##############################


def test_not_asked_is_null_not_zero():
    assert answer_stats.engine_stats([]) == {"named": None, "share": None, "rank": None, "cities": 0, "cities_named": 0}


def test_share_is_markets_naming_over_markets_asked():
    results = [_result(market_id="m1", mentioned=True, rank=2), _result(market_id="m2"), _result(market_id="m3"), _result(market_id="m4", mentioned=True, rank=4)]
    stats = answer_stats.engine_stats(results)
    assert stats == {"named": True, "share": 0.5, "rank": 3.0, "cities": 4, "cities_named": 2}


def test_named_threshold_is_half_inclusive():
    below = [_result(market_id="m1", mentioned=True), _result(market_id="m2"), _result(market_id="m3")]
    assert answer_stats.engine_stats(below)["named"] is False
    assert answer_stats.engine_stats(below)["share"] == 0.33
    single_local_market = [_result(market_id="home", mentioned=True, rank=1)]
    assert answer_stats.engine_stats(single_local_market)["named"] is True


def test_rank_averages_only_named_results_with_a_rank():
    results = [_result(market_id="m1", mentioned=True, rank=1), _result(market_id="m2", mentioned=True, rank=None), _result(market_id="m3", mentioned=True, rank=2), _result(market_id="m4", rank=7)]
    assert answer_stats.engine_stats(results)["rank"] == 1.5
    unranked = [_result(market_id="m1", mentioned=True, rank=None)]
    assert answer_stats.engine_stats(unranked)["rank"] is None


def test_latest_result_per_market_wins():
    old = _result(market_id="m1", mentioned=True, rank=1, created_at=NOW - timedelta(days=5))
    new = _result(market_id="m1", mentioned=False, created_at=NOW - timedelta(days=1))
    stats = answer_stats.engine_stats([old, new])
    assert stats["cities"] == 1 and stats["named"] is False and stats["share"] == 0.0


##############################
# questions_stats — per-question rows, summary, previous week
##############################


def test_question_rows_named_count_and_null_when_not_asked():
    prompts = [_prompt("p1"), _prompt("p2"), _prompt("p3")]
    current = [
        _result("p1", "chatgpt", mentioned=True, rank=1),
        _result("p1", "gemini", mentioned=True, rank=2),
        _result("p1", "claude"),
        _result("p2", "chatgpt"),
    ]
    rows, _ = answer_stats.questions_stats(prompts, current, [], {}, ENGINES)
    assert rows["p1"]["named_count"] == 2
    assert rows["p1"]["engines"]["chatgpt"] == {"named": True, "share": 1.0, "rank": 1.0, "cities": 1}
    assert rows["p2"]["named_count"] == 0
    assert rows["p2"]["engines"]["gemini"] == {"named": None, "share": None, "rank": None, "cities": 0}
    assert rows["p3"]["named_count"] is None
    assert set(rows["p1"]["engines"]) == set(ENGINES)


def test_only_active_engines_are_reported():
    rows, summary = answer_stats.questions_stats([_prompt("p1")], [_result("p1", "chatgpt", mentioned=True), _result("p1", "claude", mentioned=True)], [], {}, ["claude"])
    assert list(rows["p1"]["engines"]) == ["claude"]
    assert rows["p1"]["named_count"] == 1
    assert list(summary["per_engine"]) == ["claude"]


def test_display_engines_orders_and_filters_active():
    assert answer_stats.display_engines(["claude", "gemini", "chatgpt"]) == ["chatgpt", "gemini", "claude"]
    assert answer_stats.display_engines(["claude"]) == ["claude"]
    assert answer_stats.display_engines(["claude", "perplexity"]) == ["claude", "perplexity"]


def test_summary_counts_active_questions_and_compares_previous_week():
    prompts = [_prompt("p1"), _prompt("p2"), _prompt("p3"), _prompt("retiring", is_active=False)]
    current = [
        _result("p1", "chatgpt", mentioned=True),
        _result("p2", "gemini", mentioned=True),
        _result("p3", "claude"),
        _result("retiring", "chatgpt", mentioned=True),
    ]
    previous = [_result("p1", "chatgpt", mentioned=True, created_at=NOW - timedelta(days=9)), _result("p2", "gemini", created_at=NOW - timedelta(days=9))]
    rows, summary = answer_stats.questions_stats(prompts, current, previous, {}, ENGINES)
    assert summary["questions_tracked"] == 3
    assert summary["questions_named"] == 2  # the inactive question doesn't count
    assert summary["questions_named_prev"] == 1
    assert rows["p2"]["prev_named_count"] == 0 and rows["p3"]["prev_named_count"] is None
    assert summary["week_of"] == "2026-10-12"  # Monday of the newest result's week


def test_no_previous_week_data_is_null_not_zero():
    _, summary = answer_stats.questions_stats([_prompt("p1")], [_result("p1", "chatgpt", mentioned=True)], [], {}, ENGINES)
    assert summary["questions_named_prev"] is None


def test_empty_brand_summary():
    _, summary = answer_stats.questions_stats([_prompt("p1")], [], [], {}, ENGINES)
    assert summary == {
        "week_of": None,
        "questions_tracked": 1,
        "questions_named": 0,
        "questions_named_prev": None,
        "questions_up": 0,
        "questions_down": 0,
        "named_delta": None,
        "per_engine": {engine: {"questions_named": 0, "of": 0} for engine in ENGINES},
        "biggest_gap": None,
    }


def test_per_engine_rings_count_questions_named_of_asked():
    prompts = [_prompt(f"p{index}") for index in range(4)]
    current = [_result(f"p{index}", "chatgpt", mentioned=index < 3) for index in range(4)] + [_result("p0", "gemini", mentioned=True), _result("p1", "gemini")]
    _, summary = answer_stats.questions_stats(prompts, current, [], {}, ENGINES)
    assert summary["per_engine"] == {"chatgpt": {"questions_named": 3, "of": 4}, "gemini": {"questions_named": 1, "of": 2}, "claude": {"questions_named": 0, "of": 0}}


##############################
# biggest gap
##############################


def test_biggest_gap_is_fewest_named_with_its_top_competitor():
    prompts = [_prompt("p1"), _prompt("p2")]
    claude_a = _result("p1", "claude", market_id="m1")
    claude_b = _result("p2", "claude", market_id="m1")
    chatgpt_a = _result("p1", "chatgpt", mentioned=True)
    current = [claude_a, claude_b, chatgpt_a, _result("p2", "chatgpt", mentioned=True), _result("p1", "gemini", mentioned=True), _result("p2", "gemini")]
    mentions = {
        claude_a.id: [_mention(claude_a, "Roto Pro"), _mention(claude_a, "Ace Drains")],
        claude_b.id: [_mention(claude_b, "Ace Drains")],
        chatgpt_a.id: [_mention(chatgpt_a, "Roto Pro")] * 5,  # other engines' mentions don't count
    }
    _, summary = answer_stats.questions_stats(prompts, current, [], mentions, ENGINES)
    assert summary["biggest_gap"] == {"engine": "claude", "top_competitor": "Ace Drains"}


def test_biggest_gap_ties_break_on_share_then_display_order():
    per_engine = {"chatgpt": {"questions_named": 1, "of": 2}, "gemini": {"questions_named": 1, "of": 4}, "claude": {"questions_named": 1, "of": 4}}
    assert answer_stats.biggest_gap(per_engine, {}, ENGINES) == {"engine": "gemini", "top_competitor": None}


def test_no_gap_when_every_asking_engine_names_everywhere():
    per_engine = {"chatgpt": {"questions_named": 2, "of": 2}, "gemini": {"questions_named": 2, "of": 2}, "claude": {"questions_named": 0, "of": 0}}
    assert answer_stats.biggest_gap(per_engine, {"chatgpt": ["X"]}, ENGINES) is None


def test_top_competitor_ties_are_alphabetical():
    assert answer_stats.top_competitor(["Zed Plumbing", "Ace Drains", "Zed Plumbing", "Ace Drains"]) == "Ace Drains"
    assert answer_stats.top_competitor([]) is None


##############################
# week windows
##############################


def test_week_windows_are_adjacent_seven_day_spans():
    since, until = answer_stats.week_window(NOW, 0)
    prev_since, prev_until = answer_stats.week_window(NOW, 1)
    assert (since, until) == (NOW - timedelta(days=7), NOW)
    assert prev_until == since and prev_since == NOW - timedelta(days=14)


def test_week_of_is_monday_of_newest_result():
    assert answer_stats.week_of([_result(created_at=datetime(2026, 10, 9, 3)), _result(created_at=datetime(2026, 10, 11, 23))]) == date(2026, 10, 5)
    assert answer_stats.week_of([]) is None


def test_strength_sort_puts_most_named_first_and_unasked_last():
    rows = [
        {"id": "unasked", "named_count": None, "engines": {}},
        {"id": "one_low", "named_count": 1, "engines": {"chatgpt": {"share": 0.5}, "gemini": {"share": 0.0}}},
        {"id": "three", "named_count": 3, "engines": {"chatgpt": {"share": 1.0}}},
        {"id": "one_high", "named_count": 1, "engines": {"chatgpt": {"share": 1.0}, "gemini": {"share": 0.2}}},
        {"id": "zero", "named_count": 0, "engines": {"chatgpt": {"share": 0.0}}},
    ]
    assert [row["id"] for row in sorted(rows, key=answer_stats.strength_sort_key)] == ["three", "one_high", "one_low", "zero", "unasked"]


##############################
# one question's answers — sample selection + engine row
##############################


def test_sample_prefers_best_rank_then_unranked_mention():
    results = [_result(market_id="m1", mentioned=True, rank=3), _result(market_id="m2", mentioned=True, rank=1), _result(market_id="m3", mentioned=True)]
    assert answer_stats.sample_result(results, ["m1", "m2", "m3"]).market_id == "m2"
    unranked = [_result(market_id="m1"), _result(market_id="m3", mentioned=True)]
    assert answer_stats.sample_result(unranked, ["m1", "m3"]).market_id == "m3"


def test_sample_rank_ties_break_on_market_preference():
    results = [_result(market_id="far", mentioned=True, rank=1), _result(market_id="home", mentioned=True, rank=1)]
    assert answer_stats.sample_result(results, ["home", "far"]).market_id == "home"


def test_sample_never_named_uses_home_market_then_none_when_not_asked():
    results = [_result(market_id="m2"), _result(market_id="home"), _result(market_id="m3")]
    assert answer_stats.sample_result(results, ["home", "m2", "m3"]).market_id == "home"
    assert answer_stats.sample_result(results, ["unasked-home"]).market_id in {"home", "m2", "m3"}
    assert answer_stats.sample_result([], ["home"]) is None


def test_engine_answer_row_uses_corpus_answer_and_sample_mentions():
    best = _result(market_id="phx", mentioned=True, rank=2, query_response_id="qr-phx")
    other = _result(market_id="mesa", mentioned=False)
    mentions = {best.id: [_mention(best, "Zed Plumbing", position=3), _mention(best, "Ace Drains", position=1), _mention(best, "Unranked Co"), _mention(best, "Fourth Co", position=4)], other.id: [_mention(other, "Mesa Only")]}
    markets = {"phx": SimpleNamespace(city="Phoenix", state="AZ", source="system"), "mesa": SimpleNamespace(city="Mesa", state="AZ", source="local")}
    corpus = {"qr-phx": SimpleNamespace(id="qr-phx", text="x" * 2000, collected_at=datetime(2026, 10, 9, 14, 30))}
    row = answer_stats.engine_answer_row("chatgpt", [best, other], ["mesa", "phx"], mentions, markets, corpus)
    assert row["engine"] == "chatgpt"
    assert (row["named"], row["share"], row["cities_asked"], row["cities_named"], row["rank"]) == (True, 0.5, 2, 1, 2.0)
    assert row["businesses_named"] == 5  # the brand + 4 matched competitors
    assert row["also_named"] == ["Ace Drains", "Zed Plumbing", "Fourth Co"]
    assert row["sample"]["market"] == "Phoenix, AZ"
    assert row["sample"]["collected_at"] == "2026-10-09T14:30:00+00:00"
    assert row["sample"]["rank"] == 2
    assert len(row["sample"]["text"]) <= answer_stats.SAMPLE_TEXT_LIMIT + 1 and row["sample"]["text"].endswith("…")


def test_engine_answer_row_not_asked():
    row = answer_stats.engine_answer_row("claude", [], [], {}, {}, {})
    assert row == {"engine": "claude", "named": None, "share": None, "cities_asked": 0, "cities_named": 0, "rank": None, "change": None, "businesses_named": None, "also_named": [], "named_instead": [], "sample": None}


def test_businesses_named_floors_at_listed_rank_and_counts_brand_only_when_named():
    assert answer_stats.businesses_named(SimpleNamespace(brand_mentioned=True, rank=5), ["A", "B"]) == 5
    assert answer_stats.businesses_named(SimpleNamespace(brand_mentioned=False, rank=None), ["A", "B"]) == 2


def test_market_label_and_truncation():
    assert answer_stats.market_label(SimpleNamespace(city="Boise", state="ID", source="system")) == "Boise, ID"
    assert answer_stats.market_label(SimpleNamespace(city="-", state="-", source="national")) == "Nationwide"
    assert answer_stats.market_label(None) is None
    assert answer_stats.truncate_answer("short answer") == "short answer"
    long_text = ("word " * 400).strip()
    truncated = answer_stats.truncate_answer(long_text, limit=100)
    assert truncated.endswith("…") and len(truncated) <= 101 and not truncated[:-1].endswith(" ")
