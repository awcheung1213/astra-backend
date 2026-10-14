"""The automatic question funnel (utils/question_tiers.py, the weekly sorter,
the owner overrides) and the dead zone + noise gate (utils/answer_stats.py).
Pure: SimpleNamespace rows, collaborators monkeypatched — no DB, no network."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import utils.prompt_signoff as prompt_signoff
import utils.question_tiers as question_tiers
from api import recap as recap_api
from utils import answer_stats
from utils.question_tiers import (
    CORE_SLOTS,
    LONGTAIL_CHECKS_PER_MONTH,
    REASON_DEMOTED_DEAD,
    REASON_DEMOTED_ROOM,
    REASON_DEMOTED_WON,
    REASON_FILLED_OPEN,
    REASON_PROMOTED_COMPETITOR,
    REASON_PROMOTED_NAMED,
    REASON_WATCHING_ADDED,
    REASON_WATCHING_QUIET,
    WATCHING_POOL_TARGET,
    CheckSummary,
    QuestionState,
    QuestionTextError,
)
from worker.tasks import question_sorter

NOW = datetime(2026, 10, 14, 12, 0)  # a Wednesday
T0 = datetime(2026, 9, 1, 9, 0)
ENGINES = ["chatgpt", "gemini", "claude"]
_ids = iter(range(1, 1_000_000))


def _watching(prompt_id, last_checked_at=None, pending=None, offset=0):
    return SimpleNamespace(id=prompt_id, tier="watching", last_checked_at=last_checked_at, pending_signal=pending, created_at=T0 + timedelta(microseconds=offset))


##############################
# rotation slice math
##############################


def test_weeks_left_counts_the_remaining_collection_weekdays():
    # October 2026's Thursdays: 1, 8, 15, 22, 29
    assert [question_tiers.weeks_left_in_month(datetime(2026, 10, day)) for day in (1, 8, 15, 22, 29)] == [5, 4, 3, 2, 1]
    assert question_tiers.weeks_left_in_month(datetime(2026, 2, 26)) == 1  # 28-day month, last week


def test_slice_size_spreads_the_month_evenly_and_lands_on_thirty():
    checked = 0
    for day in (1, 8, 15, 22, 29):
        size = question_tiers.slice_size(datetime(2026, 10, day), checked)
        checked += size
    assert checked == LONGTAIL_CHECKS_PER_MONTH
    assert question_tiers.slice_size(datetime(2026, 10, 1), 0) == 6
    assert question_tiers.slice_size(datetime(2026, 10, 29), 30) == 0  # budget spent: nothing more this month


def test_rotation_never_checked_first_then_oldest():
    watching = [_watching("recent", datetime(2026, 9, 20), offset=0), _watching("never-b", offset=2), _watching("old", datetime(2026, 8, 1), offset=1), _watching("never-a", offset=1)]
    this_week, picks = question_tiers.rotation_slice(watching, datetime(2026, 10, 29, 0, 5))  # last week of the month: up to 30 at once
    assert picks == ["never-a", "never-b", "old", "recent"]
    assert this_week == picks


def test_rotation_is_idempotent_within_a_week():
    watching = [_watching(f"w{index}", offset=index) for index in range(40)]
    thursday = datetime(2026, 10, 8, 0, 5)
    first, picks = question_tiers.rotation_slice(watching, thursday)
    assert len(first) == len(picks) == 8  # ceil(30 / 4 weeks left)
    for prompt in watching:
        if prompt.id in picks:
            prompt.last_checked_at = thursday  # what questions_to_collect stamps
    saturday_again, new_picks = question_tiers.rotation_slice(watching, datetime(2026, 10, 10, 6))
    assert sorted(saturday_again) == sorted(first) and new_picks == []


def test_rotation_reserves_slots_for_pending_signals():
    watching = [_watching(f"w{index}", offset=index) for index in range(20)] + [_watching("pending", datetime(2026, 10, 1), pending="named", offset=99)]
    _, picks = question_tiers.rotation_slice(watching, datetime(2026, 10, 8, 0, 5))
    assert picks[0] == "pending"  # checked last week, re-checked first to confirm its signal
    assert len(picks) == 8


def test_rotation_counts_earlier_checks_this_month_only():
    watching = [_watching(f"sept{index}", datetime(2026, 9, 24), offset=index) for index in range(10)] + [_watching(f"oct{index}", datetime(2026, 10, 1), offset=50 + index) for index in range(24)]
    _, picks = question_tiers.rotation_slice(watching, datetime(2026, 10, 29, 0, 5))  # 24 done this month -> 6 left
    assert len(picks) == 6 and all(pick.startswith("sept") for pick in picks)


async def test_questions_to_collect_stamps_core_and_new_picks(monkeypatch):
    prompts = [
        SimpleNamespace(id="c2", tier="core", created_at=T0 + timedelta(microseconds=2)),
        SimpleNamespace(id="c1", tier="core", created_at=T0 + timedelta(microseconds=1)),
        _watching("w1", offset=5),
        SimpleNamespace(id="s1", tier="suggested", created_at=T0),
    ]
    stamped: list = []

    async def fake_by_brand(brand_id):
        return prompts

    async def fake_mark_checked(brand_id, ids, at):
        stamped.append((list(ids), at))

    monkeypatch.setattr(question_tiers.Prompt, "by_brand", fake_by_brand)
    monkeypatch.setattr(question_tiers.Prompt, "mark_checked", fake_mark_checked)
    plan = await question_tiers.questions_to_collect("b1", NOW)
    assert plan.core_ids == ["c1", "c2"] and plan.longtail_ids == ["w1"]
    assert stamped == [(["c1", "c2", "w1"], NOW)]
    await question_tiers.questions_to_collect("b1", NOW, record=False)
    assert len(stamped) == 1  # a preview stamps nothing


##############################
# checks, signals, value
##############################


def _stats(named, cities_named=0, cities=10):
    if named is None:
        return answer_stats.not_asked()
    return {"named": named, "share": round(cities_named / cities, 2), "rank": None, "cities": cities, "cities_named": cities_named}


def _check(named=(False, False, False), cities_named=(0, 0, 0), top_competitor_share=0.0, any_competitor=None):
    engines = {engine: _stats(flag, count) for engine, flag, count in zip(ENGINES, named, cities_named)}
    return CheckSummary(engines=engines, answers=30, top_competitor_share=top_competitor_share, any_competitor=top_competitor_share > 0 if any_competitor is None else any_competitor)


def test_check_summary_counts_the_most_named_competitor_once_per_answer():
    answers = [SimpleNamespace(id=f"a{index}") for index in range(4)]
    mentions = {"a0": [SimpleNamespace(normalized_name="zed")], "a1": [SimpleNamespace(normalized_name="zed"), SimpleNamespace(normalized_name="zed")], "a2": [SimpleNamespace(normalized_name="ace")]}
    check = question_tiers.check_summary({engine: _stats(False) for engine in ENGINES}, answers, mentions)
    assert check.top_competitor_share == 0.5 and check.any_competitor


def test_competitor_signal_needs_a_majority_and_the_brand_named_nowhere():
    assert question_tiers.check_signals(_check(top_competitor_share=0.6)) == {"competitor"}
    assert question_tiers.check_signals(_check(top_competitor_share=0.5)) == set()  # majority = MORE than half
    assert "competitor" not in question_tiers.check_signals(_check(named=(True, False, False), cities_named=(6, 0, 0), top_competitor_share=0.9))


def test_named_signal_is_any_share_above_zero():
    assert question_tiers.check_signals(_check(cities_named=(1, 0, 0))) == {"named"}


def test_promotion_needs_two_consecutive_checks():
    hit, miss = _check(top_competitor_share=0.8), _check()
    assert question_tiers.promotion_signal([hit]) is None
    assert question_tiers.promotion_signal([hit, miss]) is None
    assert question_tiers.promotion_signal([miss, hit]) is None
    assert question_tiers.promotion_signal([hit, hit]) == "competitor"
    assert question_tiers.pending_signal([hit, miss]) == "competitor"


def test_demand_promotes_only_on_both_search_console_halves():
    assert question_tiers.promotion_signal([], demand_recent=True, demand_prior=False) is None
    assert question_tiers.promotion_signal([], demand_recent=True, demand_prior=True) == "demand"
    assert question_tiers.pending_signal([], demand_recent=True) == "demand"


def test_competitor_outranks_named_when_both_confirm():
    both = _check(cities_named=(1, 0, 0), top_competitor_share=0.9)
    assert question_tiers.promotion_signal([both, both]) == "competitor"


def test_demotion_won_and_dead_need_eight_straight_weeks():
    won = _check(named=(True, True, True), cities_named=(9, 9, 9))
    dead = _check()
    assert question_tiers.demotion_signal([won] * 8) == REASON_DEMOTED_WON
    assert question_tiers.demotion_signal([dead] * 8) == REASON_DEMOTED_DEAD
    assert question_tiers.demotion_signal([won] * 7) is None
    assert question_tiers.demotion_signal([won] * 4 + [None] + [won] * 4) is None  # a missed week breaks the run
    assert question_tiers.demotion_signal([won] * 7 + [_check(named=(True, True, False), cities_named=(9, 9, 2))]) is None
    assert question_tiers.demotion_signal([dead] * 7 + [_check(top_competitor_share=0.1)]) is None  # a competitor named = not dead


def test_quiet_is_two_checks_naming_no_business():
    assert question_tiers.is_quiet([_check(), _check()])
    assert not question_tiers.is_quiet([_check()])
    assert not question_tiers.is_quiet([_check(), _check(top_competitor_share=0.2)])


def test_value_scores_worth_fighting_for():
    assert question_tiers.question_value(None) is None
    assert question_tiers.question_value(None, demand=True) == 1.0
    assert question_tiers.question_value(_check(top_competitor_share=0.8)) == 2.8  # competitor majority, brand absent
    assert question_tiers.question_value(_check(named=(True, False, False), cities_named=(6, 0, 0), top_competitor_share=0.4)) == 1.4  # winnable
    assert question_tiers.question_value(_check(named=(True, True, True), cities_named=(9, 9, 9))) == 0.0  # won
    assert question_tiers.question_value(_check()) == 0.0  # dead


def test_demand_match_needs_every_query_word_in_the_question():
    assert question_tiers.demand_match("Who does emergency water heater repair on weekends?", ["water heater repair"])
    assert question_tiers.demand_match("Best plumbers for drain cleaning", ["drain cleaning plumber"])  # plural folded
    assert not question_tiers.demand_match("Who does water heater repair?", ["tankless water heater"])
    assert not question_tiers.demand_match("Who unclogs drains?", ["plumber near me"])  # fewer than two content words


##############################
# plan_moves — every rule + guard
##############################


def _q(prompt_id, tier, offset=0, **fields):
    return QuestionState(id=prompt_id, tier=tier, created_at=T0 + timedelta(microseconds=offset), **fields)


def _full_core(value=0.5, **fields):
    return [_q(f"core{index}", "core", index, value=value, **fields) for index in range(CORE_SLOTS)]


def _moves(moves):
    return [(move.prompt_id, move.from_tier, move.to_tier, move.reason_code) for move in moves]


def test_competitor_promotion_makes_room_by_lowest_value():
    core = _full_core(value=1.0)
    core[3].value = 0.1
    candidate = _q("w1", "watching", 10, promotion="competitor", value=2.8)
    moves = question_tiers.plan_moves(core + [candidate], NOW)
    assert _moves(moves)[:2] == [("core3", "core", "watching", REASON_DEMOTED_ROOM), ("w1", "watching", "core", REASON_PROMOTED_COMPETITOR)]


def test_a_newcomer_worth_less_than_every_core_question_stays_put():
    moves = question_tiers.plan_moves(_full_core(value=1.5) + [_q("w1", "watching", promotion="named", value=1.0)], NOW)
    assert not [move for move in moves if move.to_tier == "core"]


def test_won_or_dead_core_question_gives_its_spot_first():
    core = _full_core(value=0.0)
    core[1].demotion = REASON_DEMOTED_WON
    moves = question_tiers.plan_moves(core + [_q("w1", "watching", promotion="named", value=1.0)], NOW)
    assert _moves(moves)[:2] == [("core1", "core", "watching", REASON_DEMOTED_WON), ("w1", "watching", "core", REASON_PROMOTED_NAMED)]


def test_dead_core_question_is_swapped_for_the_best_watching_one_without_a_signal():
    core = _full_core()
    core[0].demotion = REASON_DEMOTED_DEAD
    moves = question_tiers.plan_moves(core + [_q("w-low", "watching", 1, value=0.2), _q("w-high", "watching", 2, value=0.9)], NOW)
    assert _moves(moves)[:2] == [("core0", "core", "watching", REASON_DEMOTED_DEAD), ("w-high", "watching", "core", REASON_FILLED_OPEN)]


def test_open_spots_fill_without_spending_the_budget():
    questions = _full_core()[:3] + [_q("w-signal", "watching", 5, promotion="named", value=0.3), _q("w-plain", "watching", 1, value=0.9), _q("w-none", "watching", 2)]
    moves = question_tiers.plan_moves(questions, NOW, swaps_already=2)
    assert _moves(moves)[:2] == [("w-signal", "watching", "core", REASON_PROMOTED_NAMED), ("w-plain", "watching", "core", REASON_FILLED_OPEN)]


def test_at_most_two_swaps_a_month():
    core = _full_core(value=0.0)
    candidates = [_q(f"w{index}", "watching", 10 + index, promotion="competitor", value=2.5) for index in range(4)]
    moves = question_tiers.plan_moves(core + candidates, NOW)
    assert sum(1 for move in moves if move.is_swap) == 2
    assert question_tiers.plan_moves(_full_core(value=0.0) + [_q("w9", "watching", promotion="competitor", value=2.5)], NOW, swaps_already=2) == []
    one_left = question_tiers.plan_moves(_full_core(value=0.0) + [_q(f"x{index}", "watching", index, promotion="competitor", value=2.5) for index in range(3)], NOW, swaps_already=1)
    assert sum(1 for move in one_left if move.is_swap) == 1


def test_swaps_used_counts_automatic_moves_out_of_core():
    moves = [SimpleNamespace(automatic=True, from_tier="core"), SimpleNamespace(automatic=False, from_tier="core"), SimpleNamespace(automatic=True, from_tier="watching")]
    assert question_tiers.swaps_used(moves) == 1


def test_pinned_and_owner_written_questions_never_move():
    core = _full_core(value=0.0, pinned=True)
    core[0].pinned, core[0].owner_written = False, True
    core[1].pinned, core[1].demotion = True, REASON_DEMOTED_DEAD
    watching = [_q("w-pinned", "watching", promotion="competitor", value=3.0, pinned=True), _q("w-owner-quiet", "watching", quiet=True, owner_written=True), _q("w1", "watching", promotion="competitor", value=3.0)]
    moves = question_tiers.plan_moves(core + watching, NOW)
    moved = {move.prompt_id for move in moves}
    assert not moved & {"core0", "core1", "w-pinned", "w-owner-quiet"}
    assert "w1" not in moved  # no movable core question to make room


def test_a_question_just_moved_into_core_is_not_bumped_for_room():
    core = _full_core(value=1.0)
    core[2].value, core[2].tier_changed_at = 0.0, NOW - timedelta(days=6)
    moves = question_tiers.plan_moves(core + [_q("w1", "watching", promotion="competitor", value=2.9)], NOW)
    assert ("core2", "core", "watching", REASON_DEMOTED_ROOM) not in _moves(moves)
    assert ("core4", "core", "watching", REASON_DEMOTED_ROOM) in _moves(moves)  # among the settled ties, the lowest research rank goes


def test_unmeasured_core_questions_are_never_bumped_for_room():
    core = _full_core(value=None)
    assert [move for move in question_tiers.plan_moves(core + [_q("w1", "watching", promotion="competitor", value=2.9)], NOW) if move.is_swap] == []


def test_quiet_watching_goes_back_and_the_pool_tops_up_from_fresh_suggestions():
    core = _full_core()
    watching = [_q(f"w{index}", "watching", index) for index in range(WATCHING_POOL_TARGET - 2)] + [_q("quiet", "watching", 99, quiet=True)]
    newest_run = T0 + timedelta(days=30)
    suggested = [
        QuestionState(id="new-1", tier="suggested", created_at=newest_run),
        QuestionState(id="new-2", tier="suggested", created_at=newest_run + timedelta(microseconds=1)),
        QuestionState(id="old-1", tier="suggested", created_at=T0),
        QuestionState(id="stopped", tier="suggested", created_at=newest_run, tier_changed_at=NOW - timedelta(days=3)),
    ]
    moves = question_tiers.plan_moves(core + watching + suggested, NOW)
    assert ("quiet", "watching", "suggested", REASON_WATCHING_QUIET) in _moves(moves)
    added = [move.prompt_id for move in moves if move.reason_code == REASON_WATCHING_ADDED]
    assert added == ["new-1", "new-2"]  # back to 40: newest research first, rank order; a stopped/sent-back one never returns


def test_move_reasons_are_plain_words():
    for code, words in question_tiers.REASONS.items():
        assert words and words[0].isupper() and not words.endswith("."), code


##############################
# the dead zone + noise gate (answer_stats)
##############################


def _result(prompt_id="p1", engine="chatgpt", market_id="m1", mentioned=False, created_at=NOW - timedelta(days=1)):
    return SimpleNamespace(id=f"r{next(_ids)}", prompt_id=prompt_id, engine=engine, market_id=market_id, brand_mentioned=mentioned, rank=1 if mentioned else None, created_at=created_at, query_response_id="qr")


def _week(share_named, cities=20, weeks_back=0, prompt_id="p1", engine="chatgpt"):
    named = round(share_named * cities)
    return [_result(prompt_id, engine, f"m{index}", index < named, NOW - timedelta(days=1 + 7 * weeks_back)) for index in range(cities)]


def test_named_state_thresholds():
    assert answer_stats.named_state(0.5, None) and not answer_stats.named_state(0.49, None)
    assert not answer_stats.named_state(0.54, False) and answer_stats.named_state(0.55, False)
    assert answer_stats.named_state(0.45, True) and not answer_stats.named_state(0.44, True)


def test_hysteresis_chains_over_the_weeks():
    buckets = [_week(0.5, weeks_back=0), _week(0.6, weeks_back=1), _week(0.3, weeks_back=2)]
    history = answer_stats.prompt_history(buckets, ["chatgpt"])
    # oldest 0.3 -> not named; 0.6 >= 0.55 -> named; 0.5 stays named (>= 0.45)
    assert [week["chatgpt"]["named"] for week in reversed(history)] == [False, True, True]
    dipping = answer_stats.prompt_history([_week(0.5), _week(0.52, weeks_back=1)], ["chatgpt"])
    assert dipping[1]["chatgpt"]["named"] is True and dipping[0]["chatgpt"]["named"] is True
    entering = answer_stats.prompt_history([_week(0.5), _week(0.4, weeks_back=1)], ["chatgpt"])
    assert entering[0]["chatgpt"]["named"] is False  # 0.5 is not enough to ENTER


def test_a_gap_week_carries_the_previous_check_state():
    history = answer_stats.prompt_history([_week(0.5), [], _week(0.7, weeks_back=2)], ["chatgpt"])
    assert history[0]["chatgpt"]["named"] is True and history[1]["chatgpt"]["named"] is None


def test_noise_gate_formula():
    before = {"share": 0.5, "cities": 20, "cities_named": 10, "named": False}
    small = {"share": 0.6, "cities": 20, "cities_named": 12, "named": True}
    big = {"share": 0.8, "cities": 20, "cities_named": 16, "named": True}
    # pooled p = 22/40 -> 1.5 x sqrt(0.55 x 0.45 / 20) ~ 0.167 > 0.1
    assert not answer_stats.share_moved(before, small)
    assert answer_stats.share_moved(before, big)
    assert answer_stats.engine_change(before, small) is None and answer_stats.engine_change(before, big) == "up"
    one_city = ({"share": 0.0, "cities": 1, "cities_named": 0, "named": False}, {"share": 1.0, "cities": 1, "cities_named": 1, "named": True})
    assert answer_stats.engine_change(*one_city) == "up"  # a local brand's single city flipping counts


def test_question_change_and_gated_named_delta():
    prompts = [SimpleNamespace(id=f"p{index}", tier="core", category=None) for index in range(3)]
    current = _week(0.9, prompt_id="p0") + _week(0.56, prompt_id="p1") + _week(0.1, prompt_id="p2")
    previous = _week(0.1, weeks_back=1, prompt_id="p0") + _week(0.5, weeks_back=1, prompt_id="p1") + _week(0.9, weeks_back=1, prompt_id="p2")
    older = [_week(0.4, weeks_back=2, prompt_id="p1")]  # p1 was not named two weeks ago, so 0.5 last week did not enter
    rows, summary = answer_stats.questions_stats(prompts, current, previous, {}, ["chatgpt"], older=older)
    assert rows["p0"]["change"] == "up" and rows["p2"]["change"] == "down"
    assert rows["p1"]["named_count"] == 1 and rows["p1"]["prev_named_count"] == 0
    assert rows["p1"]["change"] is None  # 0.5 -> 0.56 flipped named but sits inside the noise
    assert summary["questions_up"] == 1 and summary["questions_down"] == 1 and summary["named_delta"] == 0
    assert summary["questions_named"] - summary["questions_named_prev"] == 1  # the raw count would have said "up 1"


def test_summary_counts_core_only_and_watching_reads_its_latest_check():
    prompts = [SimpleNamespace(id="core1", tier="core", category="recommendation"), SimpleNamespace(id="watch1", tier="watching", category="problem"), SimpleNamespace(id="sugg1", tier="suggested", category=None)]
    current = _week(0.9, prompt_id="core1") + _week(0.9, prompt_id="sugg1")
    older = [[], _week(0.8, weeks_back=3, prompt_id="watch1")]
    rows, summary = answer_stats.questions_stats(prompts, current, [], {}, ["chatgpt"], older=older)
    assert summary["questions_tracked"] == 1 and summary["questions_named"] == 1
    assert summary["per_engine"]["chatgpt"] == {"questions_named": 1, "of": 1}
    assert rows["watch1"]["named_count"] == 1  # its check three weeks ago


def test_watching_summary_block():
    month = datetime(2026, 10, 1)
    prompts = [
        SimpleNamespace(id="w1", tier="watching", category="problem", last_checked_at=month + timedelta(days=3), tier_changed_at=month + timedelta(days=2)),
        SimpleNamespace(id="w2", tier="watching", category="problem", last_checked_at=month - timedelta(days=3), tier_changed_at=month + timedelta(days=4)),
        SimpleNamespace(id="w3", tier="watching", category="", last_checked_at=None, tier_changed_at=None),
        SimpleNamespace(id="c1", tier="core", category="problem", last_checked_at=NOW, tier_changed_at=None),
    ]
    rows = {"w1": {"named_count": 1}, "w2": {"named_count": 0}, "w3": {"named_count": None}}
    moves = [SimpleNamespace(prompt_id="w2", from_tier="core", to_tier="watching", created_at=month + timedelta(days=4))]
    block = answer_stats.watching_summary(prompts, rows, NOW, moves)
    assert block == {
        "count": 3,
        "checked_this_month": 1,
        "named": 1,
        "new_this_month": 1,  # w1 (from the pool); w2 was demoted from core, not new
        "by_topic": [{"topic_group": "Fixing a problem", "count": 2, "named": 1}, {"topic_group": "Other", "count": 1, "named": 0}],
    }


def test_weekly_buckets_split_on_seven_day_windows():
    results = [_result(created_at=NOW), _result(created_at=NOW - timedelta(days=7)), _result(created_at=NOW - timedelta(days=13)), _result(created_at=NOW + timedelta(hours=1)), _result(created_at=NOW - timedelta(days=30))]
    buckets = answer_stats.weekly_buckets(results, NOW, 3)
    assert [len(bucket) for bucket in buckets] == [1, 2, 0]


##############################
# the sorter's read
##############################


def test_question_states_from_checks():
    prompts = [
        SimpleNamespace(id="w1", tier="watching", pinned=False, source="generated", created_at=T0, tier_changed_at=None, text="who fixes water heaters", pending_signal=None),
        SimpleNamespace(id="w2", tier="watching", pinned=False, source="generated", created_at=T0, tier_changed_at=None, text="drain cleaning", pending_signal=None),
        SimpleNamespace(id="c1", tier="core", pinned=False, source="user", created_at=T0, tier_changed_at=None, text="x", pending_signal=None),
    ]
    hit = _check(top_competitor_share=0.9)
    checks = {"w1": [hit, None, hit], "w2": [_check(cities_named=(2, 0, 0))], "c1": [_check()] * 8}
    states, pending = question_sorter.question_states(prompts, checks, ["water heater"], [])
    by_id = {state.id: state for state in states}
    assert by_id["w1"].promotion == "competitor" and pending["w1"] is None
    assert by_id["w2"].promotion is None and pending["w2"] == "named"
    assert by_id["c1"].demotion == REASON_DEMOTED_DEAD and by_id["c1"].owner_written
    assert by_id["w1"].value == 3.9  # competitor 2 + demand 1 + 0.9


def test_recap_moves_keep_only_moves_touching_core():
    moves = [
        SimpleNamespace(prompt_id="a", from_tier="watching", to_tier="core", reason="Competitors get named here and you don't yet"),
        SimpleNamespace(prompt_id="b", from_tier="suggested", to_tier="watching", reason="New question we started watching"),
        SimpleNamespace(prompt_id="c", from_tier="core", to_tier="watching", reason="Made room for a question worth more"),
    ]
    assert recap_api.recap_moves(moves, {"a": "Emergency plumber"}) == [
        {"prompt_id": "a", "topic": "Emergency plumber", "from": "watching", "to": "core", "reason": "Competitors get named here and you don't yet"},
        {"prompt_id": "c", "topic": None, "from": "core", "to": "watching", "reason": "Made room for a question worth more"},
    ]
    assert recap_api.named_delta({"named_delta": -1}) == -1 and recap_api.named_delta({}) is None


##############################
# owner overrides
##############################


def test_owner_tier_change_pins_and_stops():
    assert question_tiers.owner_tier_change("watching", False, "core", None) == ("core", True)
    assert question_tiers.owner_tier_change("core", False, "watching", None) == ("watching", True)
    assert question_tiers.owner_tier_change("core", True, "suggested", None) == ("suggested", False)
    assert question_tiers.owner_tier_change("core", False, "suggested", True) == ("suggested", True)  # the app's "stop": stays stopped
    assert question_tiers.owner_tier_change("core", True, None, False) == ("core", False)


def _prompt(prompt_id, tier, offset=0, pinned=False, source="generated", topic="Topic"):
    return SimpleNamespace(
        id=prompt_id, tier=tier, pinned=pinned, source=source, topic=topic, text=f"question {prompt_id}", category="recommendation", is_active=tier == "core", retired_at=None, created_at=T0 + timedelta(microseconds=offset), tier_reason=None, tier_changed_at=None, last_checked_at=None
    )


def test_replacement_for_core():
    four = [_prompt(f"c{index}", "core", index) for index in range(4)]
    assert question_tiers.replacement_for_core(four) is None
    five = four + [_prompt("c4", "core", 4)]
    assert question_tiers.replacement_for_core(five, {"c0": 2.0, "c1": 0.5, "c2": 1.0, "c3": 3.0, "c4": 0.5}).id == "c4"  # tie -> lower rank goes
    five[4].pinned = True
    assert question_tiers.replacement_for_core(five, {"c0": 2.0, "c1": 0.5, "c2": 1.0, "c3": 3.0, "c4": 0.0}).id == "c1"  # c4 is pinned
    assert question_tiers.replacement_for_core(five, {"c0": 2.0, "c1": 0.5, "c2": 1.0}).id == "c3"  # unmeasured counts as 0
    assert question_tiers.replacement_for_core(five, exclude="c0") is None  # moving one of core's own: room
    with pytest.raises(LookupError):
        question_tiers.replacement_for_core([_prompt(f"p{index}", "core", index, pinned=index % 2 == 0, source="user" if index % 2 else "generated") for index in range(5)])


def test_clean_owner_question_validates_and_strips_business_names():
    names = ["Reyes Plumbing & Drain LLC", "Roto-Rooter"]
    assert question_tiers.clean_owner_question("  is reyes plumbing & drain   good for water heaters? ", names) == "Is good for water heaters?"
    assert question_tiers.clean_owner_question("Is Roto Rooter's price fair for drain cleaning?", names) == "Is price fair for drain cleaning?"
    assert question_tiers.clean_owner_question("Who replaces water heaters fast?", names) == "Who replaces water heaters fast?"
    with pytest.raises(QuestionTextError, match="few more words"):
        question_tiers.clean_owner_question("hi", names)
    with pytest.raises(QuestionTextError, match="under 300"):
        question_tiers.clean_owner_question("a" * 301, names)
    with pytest.raises(QuestionTextError, match="without a business name"):
        question_tiers.clean_owner_question("Roto-Rooter?", names)


@pytest.fixture
def owner_db(monkeypatch):
    """An in-memory brand: prompts by id + the recorded moves / tasks."""
    state: dict = {"prompts": {}, "moves": [], "tasks": [], "created": [], "retired": []}

    async def by_brand(brand_id):
        return [prompt for prompt in state["prompts"].values() if prompt.retired_at is None]

    async def get_live(brand_id, prompt_id):
        prompt = state["prompts"].get(prompt_id)
        return prompt if prompt is not None and prompt.retired_at is None else None

    async def set_tier(brand_id, prompt_id, tier, reason, pinned=None):
        prompt = state["prompts"][prompt_id]
        prompt.tier, prompt.is_active, prompt.tier_reason = tier, tier == "core", reason
        if pinned is not None:
            prompt.pinned = pinned
        return 1

    async def set_pinned(brand_id, prompt_id, pinned):
        state["prompts"][prompt_id].pinned = pinned
        return 1

    async def create_move(**fields):
        state["moves"].append(fields)

    async def create_prompt(**fields):
        prompt = _prompt(f"new{len(state['created'])}", fields["tier"], 100, pinned=fields["pinned"], source=fields["source"], topic=None)
        prompt.text, prompt.category, prompt.tier_reason = fields["text"], fields["category"], fields.get("tier_reason")
        state["prompts"][prompt.id] = prompt
        state["created"].append(prompt)
        return prompt

    async def retire(brand_id, prompt_id):
        state["prompts"][prompt_id].retired_at = NOW
        state["retired"].append(prompt_id)
        return 1

    async def create_task(**kwargs):
        state["tasks"].append(kwargs["name"])

    async def competitors(brand_id):
        return [SimpleNamespace(name="Zed Drains")]

    async def values(brand_id, prompts, now, engines):
        return state.get("values", {})

    monkeypatch.setattr(prompt_signoff.Prompt, "by_brand", by_brand)
    monkeypatch.setattr(prompt_signoff.Prompt, "get_live", get_live)
    monkeypatch.setattr(prompt_signoff.Prompt, "set_tier", set_tier)
    monkeypatch.setattr(prompt_signoff.Prompt, "set_pinned", set_pinned)
    monkeypatch.setattr(prompt_signoff.Prompt, "create", create_prompt)
    monkeypatch.setattr(prompt_signoff.Prompt, "retire", retire)
    monkeypatch.setattr(prompt_signoff.QuestionMove, "create", create_move)
    monkeypatch.setattr(prompt_signoff, "create_task", create_task)
    monkeypatch.setattr(prompt_signoff.Competitor, "get_competitors_by_brand_id", competitors)
    monkeypatch.setattr(prompt_signoff, "core_values", values)
    return state


BRAND = SimpleNamespace(id="b1", name="Reyes Plumbing")


def _seed(state, prompts):
    state["prompts"] = {prompt.id: prompt for prompt in prompts}


async def test_patch_to_core_replaces_the_lowest_value_when_full(owner_db):
    _seed(owner_db, [_prompt(f"c{index}", "core", index) for index in range(5)] + [_prompt("w1", "watching", 9)])
    owner_db["values"] = {"c0": 2.0, "c1": 0.1, "c2": 1.0, "c3": 1.5, "c4": 0.9}
    question, replaced = await prompt_signoff.owner_update_question("b1", "w1", "core", True)
    assert question.tier == "core" and question.pinned and replaced.id == "c1"
    assert owner_db["prompts"]["c1"].tier == "watching"
    assert [(move["prompt_id"], move["reason_code"], move["automatic"]) for move in owner_db["moves"]] == [("c1", "made_room_for_owner", False), ("w1", "owner_moved", False)]
    assert owner_db["tasks"] == ["submit_prompt_execution_batch"]


async def test_patch_to_core_when_all_five_are_the_owners_is_core_full(owner_db):
    _seed(owner_db, [_prompt(f"c{index}", "core", index, pinned=True) for index in range(5)] + [_prompt("w1", "watching", 9)])
    with pytest.raises(prompt_signoff.CoreFull):
        await prompt_signoff.owner_update_question("b1", "w1", "core", True)
    assert owner_db["prompts"]["w1"].tier == "watching" and owner_db["moves"] == []


async def test_add_undo_order_never_hits_a_six_core_rejection(owner_db):
    """The app's Undo: PATCH new -> watching FIRST, then PATCH replaced -> core."""
    _seed(owner_db, [_prompt(f"c{index}", "core", index) for index in range(5)])
    question, replaced = await prompt_signoff.owner_add_question(BRAND, "Who fixes a leaking water heater fast?")
    assert question.tier == "core" and question.pinned and question.source == "user" and replaced is not None
    await prompt_signoff.owner_update_question("b1", question.id, "watching", None)
    back, bumped = await prompt_signoff.owner_update_question("b1", replaced.id, "core", None)
    assert back.tier == "core" and bumped is None
    assert sum(1 for prompt in owner_db["prompts"].values() if prompt.tier == "core") == 5


async def test_stop_keeps_the_pin_so_the_funnel_leaves_it_alone(owner_db):
    _seed(owner_db, [_prompt("c0", "core")])
    question, _ = await prompt_signoff.owner_update_question("b1", "c0", "suggested", True)
    assert question.tier == "suggested" and question.pinned is True
    assert owner_db["moves"][-1]["reason_code"] == "owner_stopped" and owner_db["tasks"] == []


async def test_unknown_question_is_not_found(owner_db):
    with pytest.raises(prompt_signoff.QuestionNotFound):
        await prompt_signoff.owner_update_question("b1", "nope", "core", None)


async def test_add_strips_names_and_lands_pinned_core_with_a_topic_job(owner_db):
    _seed(owner_db, [_prompt("c0", "core")])
    question, replaced = await prompt_signoff.owner_add_question(BRAND, "Is Zed Drains cheaper than others for drain cleaning?")
    assert question.text == "Is cheaper than others for drain cleaning?" and question.tier == "core" and replaced is None
    assert owner_db["tasks"] == ["submit_prompt_execution_batch", "backfill_question_topics"]
    assert owner_db["moves"][-1]["reason_code"] == "owner_added"


async def test_add_when_core_is_all_pinned_lands_monthly(owner_db):
    _seed(owner_db, [_prompt(f"c{index}", "core", index, pinned=True) for index in range(5)])
    question, replaced = await prompt_signoff.owner_add_question(BRAND, "Who unclogs a kitchen sink tonight?")
    assert question.tier == "watching" and question.pinned and replaced is None


async def test_add_an_existing_question_reuses_it(owner_db):
    existing = _prompt("w1", "watching")
    existing.text = "Who unclogs a kitchen sink tonight?"
    _seed(owner_db, [existing])
    question, _ = await prompt_signoff.owner_add_question(BRAND, "who unclogs a kitchen sink tonight")
    assert question.id == "w1" and question.tier == "core" and question.pinned and owner_db["created"] == []


async def test_add_rejects_bad_text(owner_db):
    with pytest.raises(QuestionTextError):
        await prompt_signoff.owner_add_question(BRAND, "   ")


async def test_edit_creates_a_new_pinned_question_and_retires_the_old(owner_db):
    _seed(owner_db, [_prompt("c0", "core"), _prompt("w1", "watching", 1)])
    question = await prompt_signoff.owner_edit_question(BRAND, "c0", "Who fixes water heaters on Sundays?")
    assert question.id != "c0" and question.tier == "core" and question.pinned and question.source == "user"
    assert owner_db["retired"] == ["c0"] and owner_db["moves"][-1]["reason_code"] == "owner_edited"
    assert sum(1 for prompt in owner_db["prompts"].values() if prompt.tier == "core" and prompt.retired_at is None) == 1


async def test_edit_to_a_duplicate_or_same_text(owner_db):
    _seed(owner_db, [_prompt("c0", "core"), _prompt("w1", "watching", 1)])
    with pytest.raises(QuestionTextError, match="already have"):
        await prompt_signoff.owner_edit_question(BRAND, "c0", "question w1")
    same = await prompt_signoff.owner_edit_question(BRAND, "c0", "Question c0")
    assert same.id == "c0" and owner_db["retired"] == []


def test_question_row_carries_the_level_keys():
    row = prompt_signoff.question_row(_prompt("c0", "core", pinned=True))
    assert {"tier", "pinned", "tier_reason", "last_checked_at", "topic_group", "is_active"} <= set(row) and row["topic_group"] == "Asking who to hire"


def test_topic_groups_read_as_owner_words():
    from types import SimpleNamespace

    from utils.answer_stats import topic_group

    assert topic_group(SimpleNamespace(category="problem")) == "Fixing a problem"
    assert topic_group(SimpleNamespace(category="Recommendation")) == "Asking who to hire"
    assert topic_group(SimpleNamespace(category="Your questions")) == "Your questions"
    assert topic_group(SimpleNamespace(category=None)) == "Other"


def test_recap_lists_only_automatic_moves():
    from types import SimpleNamespace

    from api.recap import recap_moves

    moves = [
        SimpleNamespace(prompt_id="a", from_tier="watching", to_tier="core", reason="AI assistants started naming you here", automatic=True),
        SimpleNamespace(prompt_id="b", from_tier="core", to_tier="suggested", reason="You stopped this one", automatic=False),
    ]
    assert [move["prompt_id"] for move in recap_moves(moves, {})] == ["a"]
