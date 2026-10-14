"""The owner's week, in time: which days we worked (worklog.activity_days),
what's coming up next (worklog.upcoming, read off the REAL cron expressions
in worker/tasks/schedules.py), and the Monday weekly recap's numbers + its
show-once rule (api/recap.py).

Everything above the GLUE section is PURE (rows in, dicts out — callers
fetch), unit-tested without a DB (tests/test_owner_calendar.py). Owner copy
follows the Marco test (customer-persona.md): no "prompt", "queue", "sweep"."""

from datetime import date, datetime, time, timedelta

from objects.change_set import STATUS_APPLIED, STATUS_REVERTED
from objects.site import Site
from schemas.base_schemas import utc_iso
from utils import clock
from utils.pitch_mail import sent_at as pitch_sent_at
from utils.receipts import revert_links
from worker.tasks.briefs import CLIENT_TASK_NAMES

ACTIVITY_DAYS = 7
RECAP_WINDOW_DAYS = 7
UPCOMING_LIMIT = 4
WEEKDAY_ABBREVIATIONS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# finished tasks that are NOT owner-visible work — the plumbing the done-for-you
# feed never shows (utils/receipts.py / api/reads.py _done_for_you): applies
# (a live change counts as its change set instead; a sent email as the pitch),
# collection batches, metric syncs, detector/chase ticks, and client tasks
PLUMBING_TASK_NAMES = frozenset({"change_set_apply", "send_pitch", "gbp_apply", "submit_prompt_execution_batch", "poll_batches", "metrics_ingestion", "alert_detection", "client_chase"} | CLIENT_TASK_NAMES)

# the owner-visible weekly events, keyed by their periodic task in
# worker/tasks/schedules.py (the cron itself is read from there, never copied)
CRON_COLLECTION = "schedule_submit_prompt_execution_batch"
CRON_SCORING = "schedule_visibility_run"
CRON_SITE_AUDIT = "schedule_site_audits"
CRON_RECAP = "schedule_weekly_briefs"  # Monday, after Sunday's scoring: the week-opener the recap rides
UPCOMING_EVENTS = (
    (CRON_COLLECTION, "We ask AI assistants the questions your customers ask"),
    (CRON_SCORING, "We score how often AI assistants recommend you"),
    (CRON_SITE_AUDIT, "We check your website for problems"),
    (CRON_RECAP, "Your weekly recap is ready"),
)


##############################
# CRON (the subset schedules.py uses for weekly events: minute hour * * dow)
##############################


def _cron_field(field: str, low: int, high: int) -> list[int]:
    """'*' | 'N' | 'N,M,...' -> sorted values in [low, high]."""
    if field == "*":
        return list(range(low, high + 1))
    values = sorted({int(part) for part in field.split(",")})
    if not values or values[0] < low or values[-1] > high:
        raise ValueError(f"cron field {field!r} out of range {low}-{high}")
    return values


def parse_weekly_cron(expression: str) -> tuple[list[int], list[int], set[int]]:
    """(minutes, hours, python weekdays Mon=0) for a 'minute hour * * dow'
    cron. Cron's dow is Sun=0 (7 also Sunday). ValueError for anything else
    (a day-of-month or month restriction is not a weekly event)."""
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError(f"cron {expression!r} must have 5 fields")
    minute, hour, day_of_month, month, day_of_week = fields
    if day_of_month != "*" or month != "*":
        raise ValueError(f"cron {expression!r} is not weekly (day-of-month/month restricted)")
    weekdays = {(cron_day - 1) % 7 for cron_day in _cron_field(day_of_week, 0, 7)}
    return _cron_field(minute, 0, 59), _cron_field(hour, 0, 23), weekdays


def next_occurrence(expression: str, after: datetime) -> datetime:
    """The first moment strictly after `after` the weekly cron fires."""
    minutes, hours, weekdays = parse_weekly_cron(expression)
    for day_offset in range(8):
        day = after.date() + timedelta(days=day_offset)
        if day.weekday() not in weekdays:
            continue
        for hour in hours:
            for minute in minutes:
                moment = datetime.combine(day, time(hour, minute))
                if moment > after:
                    return moment
    raise ValueError(f"cron {expression!r} never fires")  # unreachable for a valid weekly cron


def upcoming(crons: dict[str, str], now: datetime, include_site_audit: bool = True, limit: int = UPCOMING_LIMIT) -> list[dict]:
    """The next occurrence of each owner-visible weekly event, soonest first.
    `crons` = periodic task name -> its cron (from the live registry).
    The site audit only runs for a connected site, so it is left out without one."""
    rows = []
    for task_name, what in UPCOMING_EVENTS:
        if task_name == CRON_SITE_AUDIT and not include_site_audit:
            continue
        expression = crons.get(task_name)
        if not expression:
            continue
        moment = next_occurrence(expression, now)
        rows.append({"date": moment.date().isoformat(), "weekday": WEEKDAY_ABBREVIATIONS[moment.weekday()], "what": what, "at": utc_iso(moment)})
    rows.sort(key=lambda row: row["at"])
    return rows[:limit]


##############################
# ACTIVITY (which days we worked)
##############################


def forward_applied_change_sets(change_sets: list) -> list:
    """Change sets that went live as forward work — undos never count as work done for you."""
    undo_ids = set(revert_links(change_sets))
    return [change_set for change_set in change_sets if change_set.applied_at is not None and change_set.id not in undo_ids and change_set.status in (STATUS_APPLIED, STATUS_REVERTED)]


def activity_days(done_tasks: list, change_sets: list, today: date, days: int = ACTIVITY_DAYS) -> list[dict]:
    """The last `days` calendar days ending today, oldest first: {date, worked,
    count}. count = owner-visible tasks finished that day (plumbing excluded)
    + change sets that went live that day."""
    window = [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    counts = {day: 0 for day in window}
    for task in done_tasks:
        if task.name not in PLUMBING_TASK_NAMES and task.finished_at is not None and task.finished_at.date() in counts:
            counts[task.finished_at.date()] += 1
    for change_set in forward_applied_change_sets(change_sets):
        if change_set.applied_at.date() in counts:
            counts[change_set.applied_at.date()] += 1
    return [{"date": day.isoformat(), "worked": counts[day] > 0, "count": counts[day]} for day in window]


##############################
# WEEKLY RECAP
##############################


def week_monday(day: date) -> date:
    """The Monday that starts `day`'s ISO week."""
    return day - timedelta(days=day.weekday())


def recap_ready_at(week_of: date, recap_cron: str | None) -> datetime:
    """When this week's recap becomes ready: the recap cron's first firing in
    the week (Monday after scoring); Monday 00:00 if the cron is missing."""
    monday_midnight = datetime.combine(week_of, time())
    if not recap_cron:
        return monday_midnight
    return next_occurrence(recap_cron, monday_midnight - timedelta(microseconds=1))


def count_fixes_live(change_sets: list, since: datetime) -> int:
    """Forward change sets that went live since `since` and are STILL live —
    the same rule as Today's "fixes live this week" (receipts.change_outcomes:
    an undone fix reads "Undid: …", never as a fix). Undos excluded."""
    return sum(1 for change_set in forward_applied_change_sets(change_sets) if change_set.applied_at >= since and change_set.status == STATUS_APPLIED and not change_set.reverted_by_change_set_id)


def count_emails_sent(pitches: list, since: datetime) -> int:
    """Pitches whose content.send.status is "sent" with sent_at since `since`."""
    return sum(1 for pitch in pitches if (moment := pitch_sent_at(pitch.content)) is not None and moment >= since)


def should_show_recap(now: datetime, ready_at: datetime, seen_week: date | None, week_of: date, any_work: bool) -> bool:
    """Show once per ISO week: after it's ready (Monday-or-later, past the
    recap cron), until this user marks the week seen, and only if we did
    anything in the last 7 days."""
    seen_this_week = seen_week is not None and seen_week >= week_of
    return now >= ready_at and not seen_this_week and any_work


def recap(now: datetime, done_tasks: list, change_sets: list, pitches: list, seen_week: date | None, recap_cron: str | None, questions_named_delta: int | None = None) -> dict:
    """The weekly recap payload (GET /brands/{id}/recap)."""
    since = now - timedelta(days=RECAP_WINDOW_DAYS)
    week_of = week_monday(now.date())
    days = activity_days(done_tasks, change_sets, now.date(), days=RECAP_WINDOW_DAYS)
    fixes_live = count_fixes_live(change_sets, since)
    emails_sent = count_emails_sent(pitches, since)
    work_days = sum(1 for day in days if day["worked"])
    any_work = work_days > 0 or fixes_live > 0 or emails_sent > 0
    return {
        "week_of": week_of.isoformat(),
        "fixes_live": fixes_live,
        "emails_sent": emails_sent,
        "work_days": work_days,
        "questions_named_delta": questions_named_delta,
        "should_show": should_show_recap(now, recap_ready_at(week_of, recap_cron), seen_week, week_of, any_work),
    }


##############################
# GLUE (registry + DB)
##############################


def registered_crons() -> dict[str, str]:
    """Periodic task name -> cron, straight from procrastinate's registry as
    worker/tasks/schedules.py declared them (importing it registers them;
    imported lazily — schedules pulls in the whole worker task graph)."""
    import worker.tasks.schedules  # noqa: F401
    from worker.app import app

    return {task_name: periodic.cron for (task_name, _periodic_id), periodic in app.periodic_registry.periodic_tasks.items()}


async def upcoming_for_brand(brand_id: str) -> list[dict]:
    return upcoming(registered_crons(), clock.now(), include_site_audit=await Site.by_brand(brand_id) is not None)
