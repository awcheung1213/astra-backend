"""Framework 1.0.0 calculators — pure functions, hand-computed expectations."""

from datetime import datetime

import pytest

from schemas.prompt_result_schemas import PromptResultWithCompetitorMentions
from worker.tasks.visibility import citation_rate, detection_rate, overall_score, positive_rate, share_of_voice, top3_rate, week_of_today


def result(mentioned=False, positive=None, rank=None, cited=False, competitor_mentions=0, market_id="m1"):
    return PromptResultWithCompetitorMentions(
        id="r",
        prompt_id="p",
        market_id=market_id,
        brand_id="b",
        engine="claude",
        text="",
        raw="",
        brand_cited=cited,
        brand_mentioned=mentioned,
        brand_mention_position=rank,
        positive_sentiment=positive,
        top_3_position=rank is not None and rank <= 3,
        competitor_mention_count=competitor_mentions,
        query_response_id=None,
        created_at=datetime.now(),
        competitor_mentions=[],
    )


RESULTS = [
    result(mentioned=True, positive=True, rank=1, cited=True, competitor_mentions=2),
    result(mentioned=True, positive=False, rank=5, competitor_mentions=1),
    result(mentioned=False, competitor_mentions=3),
    result(mentioned=False),
]


def test_detection_rate():
    assert detection_rate(RESULTS) == 0.5  # 2 of 4 mentioned


def test_share_of_voice():
    # 2 brand mentions vs 6 competitor mentions -> 2/8
    assert share_of_voice(RESULTS) == 0.25


def test_positive_rate_is_over_all_results():
    assert positive_rate(RESULTS) == 0.25  # 1 positive mention of 4 results


def test_top3_rate_is_over_all_results():
    assert top3_rate(RESULTS) == 0.25  # rank 1 counts, rank 5 does not


def test_citation_rate():
    assert citation_rate(RESULTS) == 0.25


def test_overall_is_the_weighted_sum():
    expected = 0.5 * 0.40 + 0.25 * 0.25 + 0.25 * 0.15 + 0.25 * 0.15 + 0.25 * 0.05
    assert overall_score(RESULTS) == pytest.approx(expected)


def test_empty_results_score_zero():
    assert overall_score([]) == 0.0


def test_week_of_today_is_a_monday():
    assert week_of_today().weekday() == 0
