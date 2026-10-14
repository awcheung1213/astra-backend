"""Site traffic, the second proof on Today (beside "AI names you"): how many
people visited, whether that's moving, and how many came from AI assistants.

Pure functions over metric_points series (worker/tasks/metrics.py writes
them); api/reads.py feeds them. Rolling 28-day windows, never calendar
months — "this month" on the 3rd reads like a crash. The best source wins:
GA4 sessions ("visits"), else GSC clicks ("clicks from Google"). AI visits
need GA4's session source, so they're absent (never 0) without it."""

import re
from datetime import date, timedelta

WINDOW_DAYS = 28
WEEKS_SHOWN = 12
BASELINE_DAYS = 28  # the "before Evergreen" baseline: the 4 weeks before joining
MIN_DAYS_SINCE_JOINED = 28  # "since you joined" only once a full window has passed
CHANGE_MIN_SHARE = 0.10  # up/down only past both gates — the AI numbers' dead-zone idea
CHANGE_MIN_COUNT = 20

STATE_NOT_CONNECTED = "not_connected"
STATE_LOADING = "loading"
STATE_READY = "ready"

SOURCE_GA4 = "ga4"
SOURCE_GSC = "gsc"
SERIES_KEY = {SOURCE_GA4: "ga4:sessions", SOURCE_GSC: "gsc:clicks"}
AI_SERIES_KEY = "ga4:ai_sessions"
AI_BY_ASSISTANT_METRIC = "ai_sessions_by_assistant"

# GA4 sessionSource -> the assistant's name. ChatGPT tags its links
# (utm_source=chatgpt.com); the rest arrive by referrer when they arrive at
# all — visits with neither land in Direct, hence "at least" in the UI.
AI_ASSISTANT_SOURCES = {
    "chatgpt.com": "ChatGPT",
    "chat.openai.com": "ChatGPT",
    "perplexity.ai": "Perplexity",
    "claude.ai": "Claude",
    "gemini.google.com": "Gemini",
    "copilot.microsoft.com": "Copilot",
}
AI_SOURCE_REGEX = r"(www\.)?(" + "|".join(re.escape(host) for host in AI_ASSISTANT_SOURCES) + r")"


def assistant_for_source(source: str) -> str | None:
    """'www.perplexity.ai' -> 'Perplexity'; None for anything else. Pure."""
    host = (source or "").lower().strip().removeprefix("www.")
    return AI_ASSISTANT_SOURCES.get(host)


def pick_source(connected: set[str], synced: set[str]) -> tuple[str | None, str]:
    """(source, state): GA4 beats GSC; a connected source that hasn't
    finished its first sync is loading, not zero. Pure."""
    for source in (SOURCE_GA4, SOURCE_GSC):
        if source in connected:
            return source, (STATE_READY if source in synced else STATE_LOADING)
    return None, STATE_NOT_CONNECTED


def window_total(series: dict[date, float], start: date, end: date) -> float:
    """Sum of days in [start, end], both inclusive. Pure."""
    return sum(value for day, value in series.items() if start <= day <= end)


def change_word(total: float, previous: float | None) -> str | None:
    """'up' | 'down' | 'steady' past the noise gates; None without a previous window. Pure."""
    if previous is None:
        return None
    difference = total - previous
    if abs(difference) < CHANGE_MIN_COUNT:
        return "steady"
    if previous > 0 and abs(difference) / previous < CHANGE_MIN_SHARE:
        return "steady"
    return "up" if difference > 0 else "down"


def percent(new: float, old: float) -> int | None:
    return round((new - old) / old * 100) if old > 0 else None


def traffic_kpi(source: str | None, state: str, series: dict[str, dict[date, float]], ai_by_assistant: dict[str, float], joined_on: date | None) -> dict:
    """Today's visits card + its detail sheet:
    {state, source, total, previous, change, change_pct, weeks[], joined_on,
    since_joined_pct, ai_visits, ai_by_assistant[], updated_through}.
    Windows end at the newest day with data (GSC runs ~2 days behind), so a
    lagging source never reads as a drop. Pure."""
    empty: dict = {"state": state, "source": source, "total": None, "previous": None, "change": None, "change_pct": None, "weeks": [], "joined_on": joined_on.isoformat() if joined_on else None, "since_joined_pct": None, "ai_visits": None, "ai_by_assistant": [], "updated_through": None}
    if source is None or state != STATE_READY:
        return empty
    daily = series.get(SERIES_KEY[source], {})
    if not daily:
        return {**empty, "total": 0, "weeks": []}

    end = max(daily)
    earliest = min(daily)
    start = end - timedelta(days=WINDOW_DAYS - 1)
    total = window_total(daily, start, end)
    previous_start = start - timedelta(days=WINDOW_DAYS)
    previous = window_total(daily, previous_start, start - timedelta(days=1)) if earliest <= previous_start else None

    weeks = []
    for index in range(WEEKS_SHOWN - 1, -1, -1):
        week_end = end - timedelta(days=7 * index)
        week_start = week_end - timedelta(days=6)
        if week_start < earliest:
            continue  # a partly-covered week would draw as a false dip
        weeks.append({"week_of": week_start.isoformat(), "value": round(window_total(daily, week_start, week_end))})

    since_joined_pct = None
    if joined_on is not None and (end - joined_on).days >= MIN_DAYS_SINCE_JOINED:
        baseline_start = joined_on - timedelta(days=BASELINE_DAYS)
        if earliest <= baseline_start:
            since_joined_pct = percent(total, window_total(daily, baseline_start, joined_on - timedelta(days=1)))

    ai_visits = None
    assistants: list[dict] = []
    if source == SOURCE_GA4:
        ai_visits = round(window_total(series.get(AI_SERIES_KEY, {}), start, end))
        assistants = [{"name": name, "visits": round(visits)} for name, visits in sorted(ai_by_assistant.items(), key=lambda item: -item[1]) if round(visits) > 0]

    return {
        **empty,
        "total": round(total),
        "previous": round(previous) if previous is not None else None,
        "change": change_word(total, previous),
        "change_pct": percent(total, previous) if previous is not None else None,
        "weeks": weeks,
        "since_joined_pct": since_joined_pct,
        "ai_visits": ai_visits,
        "ai_by_assistant": assistants,
        "updated_through": end.isoformat(),
    }


def window_bounds(series: dict[date, float]) -> tuple[date, date] | None:
    """The 28-day window traffic_kpi uses — the per-assistant split is summed over the same days. Pure."""
    if not series:
        return None
    end = max(series)
    return end - timedelta(days=WINDOW_DAYS - 1), end


ANALYTICS_CONNECTED = "connected"
ANALYTICS_NONE_ON_ACCOUNT = "none_on_account"  # Google said his account has no Analytics property
ANALYTICS_NOT_CONNECTED = "not_connected"


def analytics_state(connected: set[str], gsc_metas: list[dict]) -> str:
    """Whether to offer "Connect Google Analytics" (never connected) or say
    plainly that his Google account has none. Pure."""
    if SOURCE_GA4 in connected:
        return ANALYTICS_CONNECTED
    if any(meta.get("ga4_on_account") is False for meta in gsc_metas):
        return ANALYTICS_NONE_ON_ACCOUNT
    return ANALYTICS_NOT_CONNECTED
