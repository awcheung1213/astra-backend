"""The overview's traffic block end to end: a connected-but-unsynced source
reads as loading, a synced one as its rolling 28 days with AI visits.
Needs the database (run against evergreen_verify, never the dev DB)."""

import uuid
from datetime import timedelta

from objects.connection import Connection
from objects.metric_point import MetricPoint
from objects.site import STATUS_CONNECTED
from tests._auth import release_runner
from utils import clock
from utils.database import Database, ReplicaDatabase


async def _signup(client) -> str:
    await release_runner()
    payload = {"organization_name": f"test-org-{uuid.uuid4()}", "brand_name": f"test brand {uuid.uuid4()}", "website_url": "https://example.com", "footprint": "national"}
    return (await client.post("/api/v1/onboarding/signup", json=payload)).json()["brand_id"]


async def _traffic(client, brand_id: str) -> dict:
    return (await client.get(f"/api/v1/brands/{brand_id}/overview")).json()["traffic"]


async def test_traffic_block_moves_from_not_connected_to_loading_to_ready(client):
    brand_id = await _signup(client)
    assert (await _traffic(client, brand_id))["state"] == "not_connected"

    async with ReplicaDatabase().connection(), Database().connection():
        await Connection.upsert(brand_id, "ga4", STATUS_CONNECTED, None)
        await Database.session.commit()
    loading = await _traffic(client, brand_id)
    assert loading["state"] == "loading" and loading["source"] == "ga4" and loading["total"] is None

    end = clock.today() - timedelta(days=1)
    async with ReplicaDatabase().connection(), Database().connection():
        for offset in range(60):
            await MetricPoint.upsert(brand_id=brand_id, source="ga4", metric="sessions", day=end - timedelta(days=offset), value=10)
        for offset in range(7):
            await MetricPoint.upsert(brand_id=brand_id, source="ga4", metric="ai_sessions", day=end - timedelta(days=offset), value=2)
            await MetricPoint.upsert(brand_id=brand_id, source="ga4", metric="ai_sessions_by_assistant", dimension="ChatGPT", day=end - timedelta(days=offset), value=2)
        await Connection.update_meta(brand_id, "ga4", synced_at=clock.now().isoformat())
        await Database.session.commit()

    ready = await _traffic(client, brand_id)
    assert ready["state"] == "ready" and ready["total"] == 280 and ready["previous"] == 280 and ready["change"] == "steady"
    assert ready["ai_visits"] == 14 and ready["ai_by_assistant"] == [{"name": "ChatGPT", "visits": 14}]
    assert ready["updated_through"] == end.isoformat() and len(ready["weeks"]) == 8
