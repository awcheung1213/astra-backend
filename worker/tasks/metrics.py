from datetime import date, datetime, timedelta

import httpx
from sqlalchemy import select

from objects.brand import Brand
from objects.connection import Connection
from objects.finding import SEVERITY_SERIOUS, Finding
from objects.metric_point import MetricPoint
from objects.notification import KIND_ALERT, URGENCY_URGENT, Notification
from objects.task import Task
from utils.database import ReplicaDatabase
from utils.logger import logger
from utils.task_queue import create_task, task_lifecycle
from worker.app import app


@app.task(name="metrics_ingestion", queue="metrics", retry=2)
async def metrics_ingestion(task_id: str, brand_id: str) -> None:
    """#18 (dashboards) — the Monday source-poller sweep: for each CONNECTED
    read integration, pull the trailing window of daily rows into the one
    normalized metric_points store. Upserts on the (source, metric, dimension,
    day) identity, so re-syncing the window self-corrects late-arriving
    GA4/GSC data and retries are safe. No connections = a free no-op."""
    async with task_lifecycle(task_id):
        window_start = date.today() - timedelta(days=SYNC_WINDOW_DAYS)
        connections = await Connection.connected_for_brand(brand_id)

        upserted_count = 0
        for connection in connections:
            sync = SYNC_BY_PROVIDER.get(connection.provider)
            if sync is None:
                logger.warning(f"[metrics] brand {brand_id}: no sync for provider {connection.provider!r} — skipping")
                continue
            for point in await sync(connection, window_start):
                await MetricPoint.upsert(brand_id=brand_id, **point)
                upserted_count += 1
        logger.info(f"[metrics] brand {brand_id}: {upserted_count} points upserted from {len(connections)} connection(s)")


SYNC_WINDOW_DAYS = 7  # re-pulled every sweep — GA4/GSC data arrives late and revises


##############################
# PER-PROVIDER SYNCS
# Each returns [{source, metric, dimension?, day, value}, ...] for the window.
# Store metrics so that SUMMING daily values is meaningful (clicks, sessions,
# conversions, calls) — never pre-averaged values like GSC position; averages
# get their own metric name and are averaged, not summed, at read time.
##############################


async def _sync_gsc(connection: Connection, window_start: date) -> list[dict]:
    # TODO(#18): Google adapter — Search Console daily clicks/impressions, per
    #       day and per page (dimension=page url; the decay detector needs it).
    #       Position is NOT stored — decided 2026-08-29: pre-averaged, sums
    #       lie, and the AI-visibility score is the product's ranking signal.
    #       Also pull GSC MESSAGES (no push API exists — this sweep is how we
    #       ever see them) -> Finding rows with GSC_FINDING_KINDS, deduped by
    #       message id in detail, auto-resolved when GSC no longer reports
    #       them; alert_detection rings Pulse on the new ones.
    return []


async def _sync_ga4(connection: Connection, window_start: date) -> list[dict]:
    # TODO(#18): Google adapter — GA4 daily sessions, conversions, and
    #       key-event counts per day.
    return []


async def _sync_calls(connection: Connection, window_start: date) -> list[dict]:
    # TODO(#18): call-tracking provider adapter — daily call counts.
    return []


SYNC_BY_PROVIDER = {"gsc": _sync_gsc, "ga4": _sync_ga4, "calls": _sync_calls}


##############################
# DETECTORS (#19) — pure code, no LLM
##############################

# finding kinds each producer OWNS (creates + auto-resolves); the audit's set
# is AUDIT_KINDS in site_audit.py — a producer never resolves another's kinds
KIND_SITE_DOWN = "site_down"
KIND_GSC_MANUAL_ACTION = "gsc_manual_action"
KIND_GSC_INDEXING_ISSUE = "gsc_indexing_issue"
GSC_FINDING_KINDS = {KIND_GSC_MANUAL_ACTION, KIND_GSC_INDEXING_ISSUE}

SITE_CHECK_TIMEOUT_SECONDS = 10.0
ALERT_COOLDOWN_DAYS = 6  # < the weekly cadence, so a FIXED-then-broken condition still alerts
NEW_FINDING_WINDOW_DAYS = 8  # "new since the last weekly sweep", with slack

TRAFFIC_DROP_THRESHOLD = 0.4  # last week vs the week before
TRAFFIC_MIN_WEEKLY_CLICKS = 50  # below this, a 40% swing is noise, not news

DECAY_DROP_THRESHOLD = 0.5  # last week vs the page's 4-week weekly baseline
DECAY_MIN_BASELINE_CLICKS = 20  # pages that never had traffic can't decay
MAX_REFRESH_TASKS_PER_SWEEP = 3  # biggest losers first; the rest wait a week

SITE_DOWN_TITLE = "Your site appears to be down"
MANUAL_ACTION_TITLE = "Google flagged an issue with your site"
TRAFFIC_DROP_TITLE = "Search traffic dropped sharply this week"


async def _check_site_reachability(brand: Brand) -> None:
    """Site down -> one finding + one urgent alert; back up -> auto-resolve.
    The open finding is the already-alerted state, so a long outage rings
    once, not weekly."""
    reachable = await _site_is_reachable(brand.website_url)
    open_site_down_findings = [finding for finding in await Finding.open_for_brand(brand.id) if finding.kind == KIND_SITE_DOWN]

    if reachable:
        for finding in open_site_down_findings:
            await Finding.resolve(finding.id)
        return
    if open_site_down_findings:
        return  # known-down, already alerted
    await Finding.create(brand_id=brand.id, kind=KIND_SITE_DOWN, severity=SEVERITY_SERIOUS, summary=f"Site unreachable: {brand.website_url}", detail={"url": brand.website_url})
    await Notification.create(organization_id=brand.organization_id, kind=KIND_ALERT, urgency=URGENCY_URGENT, title=SITE_DOWN_TITLE, body=f"{brand.website_url} did not respond to a health check.")


async def _site_is_reachable(url: str) -> bool:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=SITE_CHECK_TIMEOUT_SECONDS, headers={"User-Agent": "AstraMonitor/1.0"}) as client:
            response = await client.get(url)
    except httpx.HTTPError:
        return False
    return response.status_code < 500


async def _alert_new_gsc_issues(brand: Brand) -> None:
    """GSC-message findings created by this sweep's ingestion (#18) ring Pulse
    urgently; old-but-still-open ones already rang."""
    new_finding_cutoff = datetime.now() - timedelta(days=NEW_FINDING_WINDOW_DAYS)
    new_gsc_findings = [finding for finding in await Finding.open_for_brand(brand.id) if finding.kind in GSC_FINDING_KINDS and finding.created_at and finding.created_at > new_finding_cutoff]
    for finding in new_gsc_findings:
        await Notification.create(organization_id=brand.organization_id, kind=KIND_ALERT, urgency=URGENCY_URGENT, title=MANUAL_ACTION_TITLE, body=finding.summary)


async def _detect_traffic_drop(brand: Brand) -> None:
    """An update-shaped change-point: total search clicks down sharply week
    over week -> urgent alert + ONE chained diagnosis (#23)."""
    today = date.today()
    recent_totals = await MetricPoint.totals_for_period(brand.id, today - timedelta(days=7), today)
    prior_totals = await MetricPoint.totals_for_period(brand.id, today - timedelta(days=14), today - timedelta(days=7))
    recent_clicks, prior_clicks = recent_totals.get("gsc:clicks", 0.0), prior_totals.get("gsc:clicks", 0.0)

    if prior_clicks < TRAFFIC_MIN_WEEKLY_CLICKS:
        return
    drop_fraction = (prior_clicks - recent_clicks) / prior_clicks
    if drop_fraction < TRAFFIC_DROP_THRESHOLD:
        return
    if await _alerted_recently(brand.organization_id, TRAFFIC_DROP_TITLE):
        return

    evidence = {"gsc_clicks_last_week": recent_clicks, "gsc_clicks_week_before": prior_clicks, "drop_fraction": round(drop_fraction, 2)}
    await Notification.create(organization_id=brand.organization_id, kind=KIND_ALERT, urgency=URGENCY_URGENT, title=TRAFFIC_DROP_TITLE, body=f"Search clicks fell {round(drop_fraction * 100)}% week over week. We're investigating.")
    if not await Task.in_flight(brand.id, "diagnosis"):
        # #23 — the chained producer the schedules comment points at
        await create_task(brand_id=brand.id, name="diagnosis", queue="planning", payload={"trigger": "traffic_drop", "evidence": evidence})


async def _detect_page_decay(brand: Brand) -> None:
    """Pages whose last week fell far below their own 4-week weekly baseline
    -> ONE chained content_refresh each (#12), biggest losers first."""
    today = date.today()
    last_week_by_page = await MetricPoint.totals_by_dimension(brand.id, "gsc", "clicks", today - timedelta(days=7), today)
    baseline_by_page = await MetricPoint.totals_by_dimension(brand.id, "gsc", "clicks", today - timedelta(days=35), today - timedelta(days=7))

    decayed_pages = []
    for page, baseline_total in baseline_by_page.items():
        weekly_baseline = baseline_total / 4
        if weekly_baseline < DECAY_MIN_BASELINE_CLICKS:
            continue
        last_week = last_week_by_page.get(page, 0.0)
        if (weekly_baseline - last_week) / weekly_baseline >= DECAY_DROP_THRESHOLD:
            decayed_pages.append({"page": page, "weekly_baseline_clicks": round(weekly_baseline, 1), "last_week_clicks": last_week})
    decayed_pages.sort(key=lambda decayed: decayed["weekly_baseline_clicks"], reverse=True)

    pages_already_in_flight = {refresh_task.payload.get("page") for refresh_task in await Task.in_flight(brand.id, "content_refresh")}
    for decayed in decayed_pages[:MAX_REFRESH_TASKS_PER_SWEEP]:
        if decayed["page"] in pages_already_in_flight:
            continue
        # #12 — the chained producer the schedules comment points at
        await create_task(brand_id=brand.id, name="content_refresh", queue="content", payload={"page": decayed["page"], "evidence": decayed})


async def _alerted_recently(organization_id: str, title: str) -> bool:
    cooldown_cutoff = datetime.now() - timedelta(days=ALERT_COOLDOWN_DAYS)
    rows = await ReplicaDatabase.session.execute(select(Notification.id).where(Notification.organization_id == organization_id).where(Notification.title == title).where(Notification.created_at > cooldown_cutoff).limit(1))  # type: ignore
    return rows.first() is not None


@app.task(name="alert_detection", queue="metrics", retry=2)
async def alert_detection(task_id: str, brand_id: str) -> None:
    """#19 — pure-code detectors over fresh signals (metric points, findings,
    one live reachability check) -> urgent Pulse + chained work:
    content_refresh per decayed page (#12), diagnosis on an update-shaped
    drop (#23). Every detector guards itself: cooldowns stop a persisting
    condition from re-ringing weekly, and in-flight checks stop the same
    chained work from stacking twice."""
    async with task_lifecycle(task_id):
        brand = await Brand.get(brand_id)
        await _check_site_reachability(brand)
        await _alert_new_gsc_issues(brand)
        await _detect_traffic_drop(brand)  # -> diagnosis (#23)
        await _detect_page_decay(brand)  # -> content_refresh (#12)
