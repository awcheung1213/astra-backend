"""Weekly-cadence window math (BUG-046/047/048) — pure unit tests, no DB.

Each test pins utils.clock to simulate the week-over-week sequence the
schedules produce, then asserts the cutoff/window the query would use."""

from datetime import date, datetime, timedelta

import pytest

from objects.query_response import FRESHNESS_WINDOW, freshness_cutoff
from utils import clock
from worker.tasks import briefs, metrics, research
from worker.tasks.briefs import CHASE_AFTER_DAYS, CHASE_COOLDOWN_DAYS, calendar_days_cutoff

THURSDAY = datetime(2026, 10, 8, 0, 0)  # the Thursday 00:00 submit tick
VISIBILITY_HORIZON = timedelta(days=7)  # PromptResult.filter's scoring horizon (unchanged)


def _pin(monkeypatch, moment: datetime) -> None:
    monkeypatch.setattr(clock, "now", lambda: moment)
    monkeypatch.setattr(clock, "today", lambda: moment.date())


##############################
# BUG-046 — collection freshness vs the weekly cadence
##############################


@pytest.mark.parametrize("ingest_delay", [timedelta(hours=1), timedelta(hours=25)])  # stub/typical poll, worst-case 24h provider batch + hourly poll
def test_weekly_submit_recollects_every_week(monkeypatch, ingest_delay):
    ingested_at = THURSDAY + ingest_delay  # collected_at / prompt_results.created_at stamp
    _pin(monkeypatch, THURSDAY + timedelta(days=7))
    assert ingested_at <= freshness_cutoff()  # next Thursday: stale -> re-collected (was 'fresh' at 7d)
    _pin(monkeypatch, THURSDAY + timedelta(days=14))
    assert THURSDAY + timedelta(days=7) + ingest_delay <= freshness_cutoff()  # and the week after


def test_window_is_strictly_shorter_than_the_cadence():
    assert FRESHNESS_WINDOW <= timedelta(days=7) - timedelta(hours=25)


def test_corpus_still_reused_within_the_week(monkeypatch):
    ingested_at = THURSDAY + timedelta(hours=1)
    for days_later in (1, 3, 4):  # a mid-week sign-off / second brand reads Thursday's answers free
        _pin(monkeypatch, THURSDAY + timedelta(days=days_later))
        assert ingested_at > freshness_cutoff()


def test_sunday_scoring_horizon_holds_only_the_latest_thursday():
    sunday = THURSDAY + timedelta(days=3)  # Sunday 00:00 visibility run
    horizon = sunday - VISIBILITY_HORIZON
    assert THURSDAY + timedelta(hours=1) > horizon  # this week's answers are scored
    assert THURSDAY + timedelta(hours=1) - timedelta(days=7) <= horizon  # last week's are not double-counted
    for week in range(1, 5):  # every Sunday has that week's Thursday answers now that each Thursday collects
        assert THURSDAY + timedelta(days=7 * week, hours=1) > sunday + timedelta(days=7 * week) - VISIBILITY_HORIZON


##############################
# BUG-047 — chase cooldown / grace in calendar days (DEC-021 A)
##############################


def test_chase_nudges_every_third_day_despite_a_slow_am_call(monkeypatch):
    nudged_at = datetime(2026, 10, 1, 9, 1, 8)  # stamped after the AM call
    for days_later, within_cooldown in ((1, True), (2, True), (3, False)):
        _pin(monkeypatch, datetime(2026, 10, 1, 9, 0, 21) + timedelta(days=days_later))  # the daily 09:00 tick
        assert (nudged_at >= calendar_days_cutoff(CHASE_COOLDOWN_DAYS)) is within_cooldown


def test_task_created_mid_afternoon_is_overdue_on_day_three(monkeypatch):
    created_at = datetime(2026, 10, 1, 14, 30)
    for days_later, overdue in ((2, False), (3, True)):
        _pin(monkeypatch, datetime(2026, 10, 1, 9, 0) + timedelta(days=days_later))
        assert (created_at < calendar_days_cutoff(CHASE_AFTER_DAYS)) is overdue


def test_cutoff_is_midnight():
    assert briefs.calendar_days_cutoff(3).time() == datetime.min.time()


##############################
# BUG-048 — GSC sweeps tile week over week
##############################


class RecordingGoogle:
    windows: list[tuple[date, date]] = []

    def __init__(self, connection) -> None:
        pass

    async def search_daily_totals(self, start_day: date, end_day: date) -> list[dict]:
        RecordingGoogle.windows.append((start_day, end_day))
        return []

    async def search_daily_clicks_by_page(self, start_day: date, end_day: date) -> list[dict]:
        return []

    async def search_top_queries(self, start_day: date, end_day: date, limit: int = 100) -> list[dict]:
        RecordingGoogle.windows.append((start_day, end_day))
        return []


def _days(window: tuple[date, date]) -> set[date]:
    start, end = window  # GSC endDate is inclusive
    return {start + timedelta(days=offset) for offset in range((end - start).days + 1)}


async def test_consecutive_monday_gsc_sweeps_leave_no_gap(monkeypatch):
    monkeypatch.setattr(metrics, "GoogleAdapter", RecordingGoogle)
    RecordingGoogle.windows = []
    monday = datetime(2026, 8, 3)  # in the past, so the real-today clamp doesn't bite
    for week in range(3):
        _pin(monkeypatch, monday + timedelta(days=7 * week))
        window_start = min(clock.today(), date.today()) - timedelta(days=metrics.SYNC_WINDOW_DAYS)  # as metrics_ingestion computes it
        await metrics._sync_gsc(None, window_start)  # type: ignore[arg-type]
    covered = set().union(*(_days(window) for window in RecordingGoogle.windows))
    first, last = RecordingGoogle.windows[0][0], RecordingGoogle.windows[-1][1]
    assert covered == _days((first, last))  # every day stored
    for previous, following in zip(RecordingGoogle.windows, RecordingGoogle.windows[1:]):
        assert following[0] <= previous[1] + timedelta(days=1)  # no gap day between sweeps
    for week, (_, end) in enumerate(RecordingGoogle.windows):
        assert end == monday.date() + timedelta(days=7 * week - metrics.GSC_DATA_LAG_DAYS)  # still stops short of provisional days


async def test_research_gsc_window_is_exactly_the_named_days(monkeypatch):
    class FakeConnection:
        provider = "gsc"

    async def connected_for_brand(brand_id):
        return [FakeConnection()]

    monkeypatch.setattr(research, "GoogleAdapter", RecordingGoogle)
    monkeypatch.setattr(research.Connection, "connected_for_brand", connected_for_brand)
    RecordingGoogle.windows = []
    _pin(monkeypatch, datetime(2026, 8, 3))

    class FakeBrand:
        id = "b"
        name = "Acme"

    await research._gsc_top_queries(FakeBrand())  # type: ignore[arg-type]
    (window,) = RecordingGoogle.windows
    assert len(_days(window)) == research.GSC_QUERY_WINDOW_DAYS
    assert window[1] == date(2026, 8, 3) - timedelta(days=metrics.GSC_DATA_LAG_DAYS)
