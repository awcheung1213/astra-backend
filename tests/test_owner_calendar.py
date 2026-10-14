"""Pure (no DB, no client fixture): the owner's week in time — weekly cron
parsing + next occurrence, worklog.upcoming (read off the REAL crons in
worker/tasks/schedules.py), worklog.activity_days, and the weekly recap's
counts + show-once rule (utils/owner_calendar.py). Rows are SimpleNamespace fakes."""

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from utils import owner_calendar

# Wednesday 2026-10-14 12:00 (naive UTC, like clock.now())
NOW = datetime(2026, 10, 14, 12, 0)
CRONS = {
    "schedule_submit_prompt_execution_batch": "0 0 * * 4",
    "schedule_visibility_run": "0 0 * * 0",
    "schedule_site_audits": "0 1 * * 0",
    "schedule_weekly_briefs": "0 6 * * 1",
    "schedule_poll_batches": "0 * * * *",
}


def _task(name="site_audit", finished_at=None, id="t1"):
    return SimpleNamespace(id=id, name=name, status="done", finished_at=finished_at)


def _change_set(id="cs1", status="applied", applied_at=None, reverted_by=None, artifact_id="a1"):
    return SimpleNamespace(id=id, status=status, applied_at=applied_at, reverted_by_change_set_id=reverted_by, artifact_id=artifact_id, created_at=applied_at)


def _pitch(status="sent", sent_at=None, route="gmail"):
    send = {"status": status, "route": route}
    if sent_at is not None:
        send["sent_at"] = sent_at.isoformat()
    return SimpleNamespace(id="p1", type="pitch", status="approved", content={"send": send})


##############################
# CRON
##############################


def test_parse_weekly_cron_maps_cron_sunday_zero_and_seven_to_python_weekday():
    assert owner_calendar.parse_weekly_cron("0 0 * * 4") == ([0], [0], {3})  # Thursday
    assert owner_calendar.parse_weekly_cron("30 1,13 * * 0") == ([30], [1, 13], {6})  # Sunday
    assert owner_calendar.parse_weekly_cron("0 6 * * 7")[2] == {6}
    assert owner_calendar.parse_weekly_cron("0 9 * * *")[2] == set(range(7))


@pytest.mark.parametrize("expression", ["0 8 1 * *", "0 10 1 1,4,7,10 *", "0 0 * *", "61 0 * * 1", "0 0 * * 8"])
def test_parse_weekly_cron_rejects_non_weekly_or_invalid(expression):
    with pytest.raises(ValueError):
        owner_calendar.parse_weekly_cron(expression)


def test_next_occurrence_is_strictly_after():
    assert owner_calendar.next_occurrence("0 0 * * 4", NOW) == datetime(2026, 10, 15, 0, 0)
    exactly_then = datetime(2026, 10, 15, 0, 0)
    assert owner_calendar.next_occurrence("0 0 * * 4", exactly_then) == datetime(2026, 10, 22, 0, 0)
    assert owner_calendar.next_occurrence("0 6 * * 3", NOW) == datetime(2026, 10, 21, 6, 0)  # today's 06:00 already passed
    assert owner_calendar.next_occurrence("0 13 * * 3", NOW) == datetime(2026, 10, 14, 13, 0)


##############################
# UPCOMING
##############################


def test_upcoming_soonest_first_with_owner_words():
    rows = owner_calendar.upcoming(CRONS, NOW)
    assert [(row["date"], row["weekday"]) for row in rows] == [("2026-10-15", "Thu"), ("2026-10-18", "Sun"), ("2026-10-18", "Sun"), ("2026-10-19", "Mon")]
    assert rows[0]["what"] == "We ask AI assistants the questions your customers ask"
    assert rows[1]["what"].startswith("We score")
    assert rows[2]["what"] == "We check your website for problems"
    assert rows[3]["what"] == "Your weekly recap is ready"
    assert rows[0]["at"] == "2026-10-15T00:00:00+00:00"
    for row in rows:
        assert not any(word in row["what"].lower() for word in ("prompt", "queue", "sweep", "batch", "cron"))


def test_upcoming_drops_site_audit_without_a_connected_site_and_caps():
    rows = owner_calendar.upcoming(CRONS, NOW, include_site_audit=False)
    assert "We check your website for problems" not in [row["what"] for row in rows]
    assert len(rows) == 3
    assert len(owner_calendar.upcoming(CRONS, NOW, limit=2)) == 2


def test_upcoming_reads_the_real_schedules_crons():
    """The crons come from procrastinate's registry as schedules.py declared
    them — the events land on the weekdays the code says, whatever they are."""
    crons = owner_calendar.registered_crons()
    for task_name, _what in owner_calendar.UPCOMING_EVENTS:
        assert task_name in crons, f"{task_name} is no longer a periodic task in worker/tasks/schedules.py"
    rows = owner_calendar.upcoming(crons, NOW)
    assert len(rows) == 4
    expected = {what: owner_calendar.next_occurrence(crons[name], NOW) for name, what in owner_calendar.UPCOMING_EVENTS}
    for row in rows:
        assert row["date"] == expected[row["what"]].date().isoformat()
        assert row["weekday"] == expected[row["what"]].strftime("%a")
    assert [row["at"] for row in rows] == sorted(row["at"] for row in rows)


##############################
# ACTIVITY DAYS
##############################


def test_activity_days_seven_days_ending_today_oldest_first():
    days = owner_calendar.activity_days([], [], NOW.date())
    assert [day["date"] for day in days] == [(NOW.date() - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)]
    assert all(day == {"date": day["date"], "worked": False, "count": 0} for day in days)


def test_activity_days_counts_owner_work_not_plumbing():
    today = NOW.date()
    tasks = [
        _task("site_audit", NOW - timedelta(hours=2), "t1"),
        _task("visibility_run", NOW - timedelta(hours=3), "t2"),
        _task("client_chase", NOW - timedelta(hours=1), "t3"),  # daily tick: plumbing
        _task("change_set_apply", NOW - timedelta(hours=1), "t4"),  # counted as its change set instead
        _task("send_pitch", NOW - timedelta(hours=1), "t5"),
        _task("metrics_ingestion", NOW - timedelta(days=2), "t6"),
        _task("collect_study_data", NOW - timedelta(days=2), "t7"),  # client task
        _task("outreach_scout", NOW - timedelta(days=2), "t8"),
        _task("site_audit", NOW - timedelta(days=9), "t9"),  # outside the window
        _task("site_audit", None, "t10"),
    ]
    change_sets = [
        _change_set("cs1", "applied", NOW - timedelta(hours=5)),
        _change_set("cs2", "reverted", NOW - timedelta(days=1), reverted_by="cs3"),  # went live yesterday, undone later: still work that day
        _change_set("cs3", "applied", NOW - timedelta(hours=1)),  # the undo itself: never counts
        _change_set("cs4", "pending", None),
        _change_set("cs5", "failed", None),
    ]
    by_date = {day["date"]: day for day in owner_calendar.activity_days(tasks, change_sets, today)}
    assert by_date[today.isoformat()] == {"date": today.isoformat(), "worked": True, "count": 3}  # audit + scoring + cs1
    assert by_date[(today - timedelta(days=1)).isoformat()]["count"] == 1  # cs2
    assert by_date[(today - timedelta(days=2)).isoformat()]["count"] == 1  # outreach scan only
    assert by_date[(today - timedelta(days=3)).isoformat()]["worked"] is False


def test_plumbing_names_cover_client_tasks_and_ticks():
    for name in ("change_set_apply", "send_pitch", "gbp_apply", "metrics_ingestion", "alert_detection", "client_chase", "grant_access", "upload_photos"):
        assert name in owner_calendar.PLUMBING_TASK_NAMES
    for name in ("site_audit", "visibility_run", "weekly_brief", "technical_fix", "content_draft", "outreach_scout"):
        assert name not in owner_calendar.PLUMBING_TASK_NAMES


##############################
# WEEKLY RECAP
##############################


def test_week_monday():
    assert owner_calendar.week_monday(date(2026, 10, 14)) == date(2026, 10, 12)
    assert owner_calendar.week_monday(date(2026, 10, 12)) == date(2026, 10, 12)
    assert owner_calendar.week_monday(date(2026, 10, 18)) == date(2026, 10, 12)  # Sunday ends the ISO week


def test_recap_ready_at_is_the_monday_recap_cron():
    assert owner_calendar.recap_ready_at(date(2026, 10, 12), "0 6 * * 1") == datetime(2026, 10, 12, 6, 0)
    assert owner_calendar.recap_ready_at(date(2026, 10, 12), "0 0 * * 1") == datetime(2026, 10, 12, 0, 0)
    assert owner_calendar.recap_ready_at(date(2026, 10, 12), None) == datetime(2026, 10, 12, 0, 0)


def test_should_show_recap_rule():
    week_of = date(2026, 10, 12)
    ready = datetime(2026, 10, 12, 6, 0)
    show = owner_calendar.should_show_recap
    assert show(NOW, ready, None, week_of, any_work=True) is True
    assert show(NOW, ready, date(2026, 10, 5), week_of, any_work=True) is True  # dismissed LAST week only
    assert show(NOW, ready, week_of, week_of, any_work=True) is False  # dismissed this week
    assert show(NOW, ready, None, week_of, any_work=False) is False  # nothing to recap
    assert show(datetime(2026, 10, 12, 5, 59), ready, None, week_of, any_work=True) is False  # Monday, before it's ready
    assert show(datetime(2026, 10, 18, 23, 0), ready, None, week_of, any_work=True) is True  # still this week on Sunday


def test_count_fixes_live_excludes_undos_and_old_and_unapplied():
    since = NOW - timedelta(days=7)
    change_sets = [
        _change_set("cs1", "applied", NOW - timedelta(days=1)),
        _change_set("cs2", "reverted", NOW - timedelta(days=2), reverted_by="cs3"),
        _change_set("cs3", "applied", NOW - timedelta(days=1)),  # undo
        _change_set("cs4", "applied", NOW - timedelta(days=8)),
        _change_set("cs5", "pending", None),
    ]
    assert owner_calendar.count_fixes_live(change_sets, since) == 2


def test_count_emails_sent_by_sent_at_in_window():
    since = NOW - timedelta(days=7)
    pitches = [
        _pitch("sent", NOW - timedelta(days=1)),
        _pitch("sent", NOW - timedelta(days=2), route="manual"),  # status sent ("I sent it") counts
        _pitch("sent", NOW - timedelta(days=10)),
        _pitch("scheduled"),
        _pitch("failed"),
        _pitch("cancelled"),
        SimpleNamespace(id="p9", type="pitch", status="approved", content={}),
    ]
    assert owner_calendar.count_emails_sent(pitches, since) == 2


def test_recap_payload_shape_and_counts():
    tasks = [_task("site_audit", NOW - timedelta(days=1)), _task("visibility_run", NOW - timedelta(days=3)), _task("client_chase", NOW - timedelta(days=4))]
    change_sets = [_change_set("cs1", "applied", NOW - timedelta(days=1))]
    pitches = [_pitch("sent", NOW - timedelta(days=2))]
    payload = owner_calendar.recap(NOW, tasks, change_sets, pitches, seen_week=None, recap_cron="0 6 * * 1")
    assert payload == {"week_of": "2026-10-12", "fixes_live": 1, "emails_sent": 1, "work_days": 2, "questions_named_delta": None, "should_show": True}
    assert owner_calendar.recap(NOW, tasks, change_sets, pitches, seen_week=date(2026, 10, 12), recap_cron="0 6 * * 1")["should_show"] is False


def test_recap_hidden_with_no_work_and_emails_alone_count_as_work():
    quiet = owner_calendar.recap(NOW, [_task("client_chase", NOW - timedelta(days=1))], [], [], seen_week=None, recap_cron="0 6 * * 1")
    assert quiet["work_days"] == 0 and quiet["should_show"] is False
    emailed = owner_calendar.recap(NOW, [], [], [_pitch("sent", NOW - timedelta(days=1))], seen_week=None, recap_cron="0 6 * * 1")
    assert emailed["emails_sent"] == 1 and emailed["should_show"] is True


##############################
# WIRING (monkeypatched — no DB)
##############################


def _returns(value):
    async def fake(*args, **kwargs):
        return value

    return fake


async def test_recap_endpoints(monkeypatch):
    import api.recap as recap_api

    marked: list = []

    async def mark(id, week_of):
        marked.append((id, week_of))

    user = SimpleNamespace(id="u1", recap_seen_week=None)
    request = SimpleNamespace(state=SimpleNamespace(current_user=user))
    monkeypatch.setattr(recap_api.clock, "now", lambda: NOW)
    monkeypatch.setattr(recap_api.clock, "today", lambda: NOW.date())
    monkeypatch.setattr(recap_api.Brand, "get", _returns(SimpleNamespace(id="b1")))
    monkeypatch.setattr(recap_api.Task, "for_brand", _returns([_task("site_audit", NOW - timedelta(days=1))]))
    monkeypatch.setattr(recap_api.ChangeSet, "for_brand", _returns([_change_set("cs1", "applied", NOW - timedelta(days=2))]))
    monkeypatch.setattr(recap_api, "questions_named_delta", _returns(None))  # covered by test_recap_questions_named_delta
    moves = [{"prompt_id": "p1", "topic": "Emergency plumber", "from": "watching", "to": "core", "reason": "Competitors get named here and you don't yet"}]
    monkeypatch.setattr(recap_api, "question_moves", _returns(moves))  # recap_moves: tests/test_question_tiers.py
    monkeypatch.setattr(recap_api.Artifact, "for_brand", _returns([_pitch("sent", NOW - timedelta(days=3))]))
    monkeypatch.setattr(recap_api.User, "mark_recap_seen", mark)

    payload = await recap_api.recap("b1", request)
    assert payload == {"week_of": "2026-10-12", "fixes_live": 1, "emails_sent": 1, "work_days": 2, "questions_named_delta": None, "should_show": True, "question_moves": moves}
    assert await recap_api.recap_seen("b1", request) == {"week_of": "2026-10-12", "seen": True}
    assert marked == [("u1", date(2026, 10, 12))]
    user.recap_seen_week = date(2026, 10, 12)
    assert (await recap_api.recap("b1", request))["should_show"] is False


def test_recap_questions_named_delta():
    from api.recap import named_delta

    # the noise-gated up minus down (utils/answer_stats.questions_stats' named_delta), not the raw count difference
    assert named_delta({"questions_named": 8, "questions_named_prev": 6, "named_delta": 1}) == 1
    assert named_delta({"questions_named": 5, "questions_named_prev": 7, "named_delta": -2}) == -2
    assert named_delta({"questions_named": 8, "questions_named_prev": None, "named_delta": None}) is None
