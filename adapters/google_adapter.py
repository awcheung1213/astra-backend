"""Google read adapter — Search Console + GA4, the metrics sweep's source.

Auth: OAuth2 refresh tokens, exchanged for a short-lived access token per
adapter instance. Encrypted credentials shape on the connection row:
- gsc:  {"refresh_token": ..., "site_url": "sc-domain:example.com" or "https://example.com/"}
- ga4:  {"refresh_token": ..., "property_id": "123456789"}
The OAuth app itself is GOOGLE_OAUTH_CLIENT_ID/SECRET from settings; the
consent flow that puts refresh tokens on connection rows is api/connections.py
(one "google" consent covers both; the two rows then share one grant).

These are free read APIs: no cost events (cost events are written manually
at spend sites; a $0 read has none).

The public Search Console API exposes neither messages nor manual actions
(verified 2026-08-30) — GSC messages are deliberately NOT a feature until a
real source is designed."""

from datetime import date, datetime
from urllib.parse import quote

import httpx

import settings
from objects.connection import Connection
from utils.token_crypto import decrypt_credentials
from utils.traffic import AI_SOURCE_REGEX, assistant_for_source

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GSC_API_BASE = "https://searchconsole.googleapis.com/webmasters/v3"
GA4_API_BASE = "https://analyticsdata.googleapis.com/v1beta"
GA4_ADMIN_API_BASE = "https://analyticsadmin.googleapis.com/v1beta"
READ_TIMEOUT_SECONDS = 30.0
GSC_ROW_LIMIT = 25000  # the API's max per request; a 90-day per-page backfill can need more, so _gsc_query pages
GSC_MAX_PAGES = 8  # 200k rows: far past any small business's pages x days


class GoogleReadError(Exception):
    """A Google read failed. Propagates out of metrics_ingestion so
    procrastinate retries (upserts make retries safe); exhausted retries
    dead-letter to the operator."""


async def fetch_google_access_token(client: httpx.AsyncClient, refresh_token: str) -> str:
    """Refresh-token -> short-lived access token, shared by every Google
    adapter (GSC/GA4 reads here, GBP writes in adapters/gbp_adapter.py)."""
    if not settings.GOOGLE_OAUTH_CLIENT_ID:
        raise GoogleReadError("GOOGLE_OAUTH_CLIENT_ID/SECRET not configured")
    try:
        response = await client.post(GOOGLE_TOKEN_URL, data={"client_id": settings.GOOGLE_OAUTH_CLIENT_ID, "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET, "refresh_token": refresh_token, "grant_type": "refresh_token"})
    except httpx.HTTPError as error:
        raise GoogleReadError(f"google token refresh failed: {error}") from error
    if response.status_code >= 400:
        raise GoogleReadError(f"google token refresh -> {response.status_code}: {response.text[:200]}")
    return response.json()["access_token"]


async def fetch_google_access_token_standalone(refresh_token: str) -> str:
    """fetch_google_access_token with its own client — for callers outside an
    adapter instance (the connect flow's property listings)."""
    async with httpx.AsyncClient(timeout=READ_TIMEOUT_SECONDS) as client:
        return await fetch_google_access_token(client, refresh_token)


async def exchange_google_code(code: str) -> dict:
    """Authorization code -> token payload (access_token + refresh_token) —
    the consent callback's half of the OAuth dance."""
    if not settings.GOOGLE_OAUTH_CLIENT_ID:
        raise GoogleReadError("GOOGLE_OAUTH_CLIENT_ID/SECRET not configured")
    async with httpx.AsyncClient(timeout=READ_TIMEOUT_SECONDS) as client:
        try:
            response = await client.post(GOOGLE_TOKEN_URL, data={"client_id": settings.GOOGLE_OAUTH_CLIENT_ID, "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET, "code": code, "grant_type": "authorization_code", "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URL})
        except httpx.HTTPError as error:
            raise GoogleReadError(f"google code exchange failed: {error}") from error
    if response.status_code >= 400:
        raise GoogleReadError(f"google code exchange -> {response.status_code}: {response.text[:200]}")
    return response.json()


async def list_gsc_properties(access_token: str) -> list[str]:
    """The GSC properties the consented account can read (unverified ones
    excluded — queries against them 403)."""
    async with httpx.AsyncClient(timeout=READ_TIMEOUT_SECONDS) as client:
        try:
            response = await client.get(f"{GSC_API_BASE}/sites", headers={"Authorization": f"Bearer {access_token}"})
        except httpx.HTTPError as error:
            raise GoogleReadError(f"gsc site listing failed: {error}") from error
    if response.status_code >= 400:
        raise GoogleReadError(f"gsc site listing -> {response.status_code}: {response.text[:200]}")
    return [entry["siteUrl"] for entry in response.json().get("siteEntry", []) if entry.get("permissionLevel") != "siteUnverifiedUser"]


async def list_ga4_properties(access_token: str) -> list[dict]:
    """The GA4 properties the consented account can read:
    [{property_id, display_name}] — no auto-match is possible, the client picks."""
    async with httpx.AsyncClient(timeout=READ_TIMEOUT_SECONDS) as client:
        try:
            response = await client.get(f"{GA4_ADMIN_API_BASE}/accountSummaries", params={"pageSize": 200}, headers={"Authorization": f"Bearer {access_token}"})
        except httpx.HTTPError as error:
            raise GoogleReadError(f"ga4 property listing failed: {error}") from error
    if response.status_code >= 400:
        raise GoogleReadError(f"ga4 property listing -> {response.status_code}: {response.text[:200]}")
    properties = []
    for account in response.json().get("accountSummaries", []):
        for summary in account.get("propertySummaries", []):
            properties.append({"property_id": summary.get("property", "").removeprefix("properties/"), "display_name": summary.get("displayName", "")})
    return properties


async def list_ga4_web_streams(access_token: str, property_id: str) -> list[dict]:
    """One property's web streams: [{measurement_id, default_uri}] — what the
    connect flow matches against the brand's website and the G- tag on its
    homepage, so the owner never picks a property."""
    async with httpx.AsyncClient(timeout=READ_TIMEOUT_SECONDS) as client:
        try:
            response = await client.get(f"{GA4_ADMIN_API_BASE}/properties/{property_id}/dataStreams", params={"pageSize": 50}, headers={"Authorization": f"Bearer {access_token}"})
        except httpx.HTTPError as error:
            raise GoogleReadError(f"ga4 stream listing failed: {error}") from error
    if response.status_code >= 400:
        raise GoogleReadError(f"ga4 stream listing -> {response.status_code}: {response.text[:200]}")
    streams = []
    for stream in response.json().get("dataStreams", []):
        web = stream.get("webStreamData") or {}
        if web:
            streams.append({"measurement_id": web.get("measurementId", ""), "default_uri": web.get("defaultUri", "")})
    return streams


async def revoke_google_token(refresh_token: str) -> None:
    """Best-effort revoke at Google on disconnect — failures are the
    caller's to log, never to surface (the row is deleted regardless)."""
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(GOOGLE_REVOKE_URL, params={"token": refresh_token})


class GoogleAdapter:
    def __init__(self, connection: Connection) -> None:
        credentials = decrypt_credentials(connection.credentials_encrypted or "")
        self.refresh_token = credentials["refresh_token"]
        self.gsc_site_url = credentials.get("site_url")
        self.ga4_property_id = credentials.get("property_id")
        self._access_token: str | None = None

    ##############################
    # SEARCH CONSOLE
    ##############################

    async def search_daily_totals(self, start_day: date, end_day: date) -> list[dict]:
        """Property-wide daily clicks + impressions: [{day, clicks, impressions}]."""
        rows = await self._gsc_query(start_day, end_day, dimensions=["date"])
        return [{"day": date.fromisoformat(row["keys"][0]), "clicks": row.get("clicks", 0), "impressions": row.get("impressions", 0)} for row in rows]

    async def search_daily_clicks_by_page(self, start_day: date, end_day: date) -> list[dict]:
        """Per-page daily clicks (the decay detector's food): [{day, page, clicks}]."""
        rows = await self._gsc_query(start_day, end_day, dimensions=["date", "page"])
        return [{"day": date.fromisoformat(row["keys"][0]), "page": row["keys"][1], "clicks": row.get("clicks", 0)} for row in rows]

    async def search_top_queries(self, start_day: date, end_day: date, limit: int = 100) -> list[dict]:
        """The real queries people used, best first (GSC sorts by clicks):
        [{query, clicks, impressions}] — prompt research's demand grounding."""
        rows = await self._gsc_query(start_day, end_day, dimensions=["query"])
        return [{"query": row["keys"][0], "clicks": row.get("clicks", 0), "impressions": row.get("impressions", 0)} for row in rows[:limit]]

    async def _gsc_query(self, start_day: date, end_day: date, dimensions: list[str]) -> list[dict]:
        if not self.gsc_site_url:
            raise GoogleReadError("connection has no GSC site_url")
        rows: list[dict] = []
        for page in range(GSC_MAX_PAGES):
            body = {"startDate": start_day.isoformat(), "endDate": end_day.isoformat(), "dimensions": dimensions, "rowLimit": GSC_ROW_LIMIT, "startRow": page * GSC_ROW_LIMIT}
            payload = await self._request("POST", f"{GSC_API_BASE}/sites/{quote(self.gsc_site_url, safe='')}/searchAnalytics/query", body)
            batch = payload.get("rows", [])
            rows += batch
            if len(batch) < GSC_ROW_LIMIT:
                break
        return rows

    ##############################
    # GA4
    ##############################

    async def analytics_daily(self, start_day: date, end_day: date) -> list[dict]:
        """Daily sessions + key events: [{day, sessions, key_events}]."""
        if not self.ga4_property_id:
            raise GoogleReadError("connection has no GA4 property_id")
        body = {
            "dateRanges": [{"startDate": start_day.isoformat(), "endDate": end_day.isoformat()}],
            "dimensions": [{"name": "date"}],
            "metrics": [{"name": "sessions"}, {"name": "keyEvents"}],
        }
        payload = await self._request("POST", f"{GA4_API_BASE}/properties/{self.ga4_property_id}:runReport", body)
        daily = []
        for row in payload.get("rows", []):
            day = datetime.strptime(row["dimensionValues"][0]["value"], "%Y%m%d").date()
            metric_values = row.get("metricValues", [])
            daily.append({"day": day, "sessions": float(metric_values[0]["value"]), "key_events": float(metric_values[1]["value"])})
        return daily

    async def analytics_ai_daily(self, start_day: date, end_day: date) -> list[dict]:
        """Daily sessions that came from AI assistants, per assistant:
        [{day, assistant, sessions}] — sessionSource matched against the
        known assistant hosts (utils/traffic.py), several hosts folding into
        one name (chatgpt.com + chat.openai.com -> ChatGPT)."""
        if not self.ga4_property_id:
            raise GoogleReadError("connection has no GA4 property_id")
        body = {
            "dateRanges": [{"startDate": start_day.isoformat(), "endDate": end_day.isoformat()}],
            "dimensions": [{"name": "date"}, {"name": "sessionSource"}],
            "metrics": [{"name": "sessions"}],
            "dimensionFilter": {"filter": {"fieldName": "sessionSource", "stringFilter": {"matchType": "FULL_REGEXP", "value": AI_SOURCE_REGEX, "caseSensitive": False}}},
        }
        payload = await self._request("POST", f"{GA4_API_BASE}/properties/{self.ga4_property_id}:runReport", body)
        totals: dict[tuple[date, str], float] = {}
        for row in payload.get("rows", []):
            assistant = assistant_for_source(row["dimensionValues"][1]["value"])
            if assistant is None:
                continue
            day = datetime.strptime(row["dimensionValues"][0]["value"], "%Y%m%d").date()
            totals[(day, assistant)] = totals.get((day, assistant), 0.0) + float(row["metricValues"][0]["value"])
        return [{"day": day, "assistant": assistant, "sessions": sessions} for (day, assistant), sessions in sorted(totals.items())]

    ##############################
    # PLUMBING
    ##############################

    async def _request(self, method: str, url: str, json_body: dict) -> dict:
        async with httpx.AsyncClient(timeout=READ_TIMEOUT_SECONDS) as client:
            access_token = await self._fetch_access_token(client)
            try:
                response = await client.request(method, url, json=json_body, headers={"Authorization": f"Bearer {access_token}"})
            except httpx.HTTPError as error:
                raise GoogleReadError(f"google {method} {url} failed: {error}") from error
        if response.status_code >= 400:
            raise GoogleReadError(f"google {method} {url} -> {response.status_code}: {response.text[:200]}")
        return response.json()

    async def _fetch_access_token(self, client: httpx.AsyncClient) -> str:
        if self._access_token is None:
            self._access_token = await fetch_google_access_token(client, self.refresh_token)
        return self._access_token
