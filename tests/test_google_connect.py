"""Google OAuth connect (gsc + ga4): the signed state is the callback's only
auth, GSC auto-connects when exactly one property matches the brand's
website (otherwise — and always for GA4 — the row lands pending for a
validated property pick), one row per (brand, provider) upserted, and
disconnect revokes + forgets. Every Google call is monkeypatched on the
adapter module — no network. Runs in compose (make test), needs the
database."""

import base64
import hashlib
import hmac
import json
import time
import uuid

from sqlalchemy import select

import settings
from adapters import google_adapter
from api.connections import mint_state
from objects.connection import Connection
from tests._auth import release_runner
from utils.database import Database, ReplicaDatabase
from utils.token_crypto import decrypt_credentials

TOKENS = {"access_token": "at-test", "refresh_token": "rt-test"}


def _stub_google(monkeypatch, gsc_properties=None, ga4_properties=None):
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "test-client")

    async def exchange(code):
        return dict(TOKENS)

    async def list_gsc(access_token):
        return list(gsc_properties or [])

    async def list_ga4(access_token):
        return list(ga4_properties or [])

    async def access_token_standalone(refresh_token):
        return "at-test"

    async def revoke(refresh_token):
        return None

    monkeypatch.setattr(google_adapter, "exchange_google_code", exchange)
    monkeypatch.setattr(google_adapter, "list_gsc_properties", list_gsc)
    monkeypatch.setattr(google_adapter, "list_ga4_properties", list_ga4)
    monkeypatch.setattr(google_adapter, "fetch_google_access_token_standalone", access_token_standalone)
    monkeypatch.setattr(google_adapter, "revoke_google_token", revoke)


async def _signup(client, website_url="https://example.com"):
    await release_runner()  # one org per user — free the runner so this signup claims fresh
    payload = {"organization_name": f"test-org-{uuid.uuid4()}", "brand_name": f"test brand {uuid.uuid4()}", "website_url": website_url, "footprint": "national"}
    return (await client.post("/api/v1/onboarding/signup", json=payload)).json()["brand_id"]


async def _connection_rows(brand_id: str) -> list[Connection]:
    async with ReplicaDatabase().connection(), Database().connection():
        rows = await ReplicaDatabase.session.execute(select(Connection).where(Connection.brand_id == brand_id))
        return list(rows.scalars().all())


async def _callback(client, brand_id: str, provider: str, **params):
    return await client.get("/api/v1/connections/google/callback", params={"state": mint_state(brand_id, provider), "code": "test-code", **params})


def _result(response) -> str:
    assert response.status_code == 302
    return response.headers["location"].rsplit("result=", 1)[1]


async def test_authorize_returns_the_consent_url(client, monkeypatch):
    _stub_google(monkeypatch)
    brand_id = await _signup(client)
    response = await client.post(f"/api/v1/brands/{brand_id}/connections/gsc/authorize")
    assert response.status_code == 200
    url = response.json()["url"]
    assert url.startswith(google_adapter.GOOGLE_AUTH_URL) and "webmasters.readonly" in url and "state=" in url and "access_type=offline" in url

    assert (await client.post(f"/api/v1/brands/{brand_id}/connections/calls/authorize")).status_code == 422
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "")
    assert (await client.post(f"/api/v1/brands/{brand_id}/connections/gsc/authorize")).status_code == 503


async def test_gsc_callback_auto_matches_the_brand_website(client, monkeypatch):
    _stub_google(monkeypatch, gsc_properties=["sc-domain:example.com", "https://unrelated.example/"])
    brand_id = await _signup(client, website_url="https://www.example.com/shop")

    assert _result(await _callback(client, brand_id, "gsc")) == "connected"

    card = (await client.get(f"/api/v1/brands/{brand_id}/connections")).json()
    assert card["providers"] == [{"provider": "gsc", "status": "connected", "connected_at": card["providers"][0]["connected_at"]}]
    rows = await _connection_rows(brand_id)
    assert decrypt_credentials(rows[0].credentials_encrypted) == {"refresh_token": "rt-test", "site_url": "sc-domain:example.com"}

    # a second consent upserts the SAME row
    assert _result(await _callback(client, brand_id, "gsc")) == "connected"
    assert len(await _connection_rows(brand_id)) == 1


async def test_gsc_callback_without_a_match_pends_then_pick_resolves(client, monkeypatch):
    _stub_google(monkeypatch, gsc_properties=["https://other.example/", "https://another.example/"])
    brand_id = await _signup(client)

    assert _result(await _callback(client, brand_id, "gsc")) == "pick_property"
    assert (await client.get(f"/api/v1/brands/{brand_id}/connections")).json()["providers"][0]["status"] == "pending"

    properties = (await client.get(f"/api/v1/brands/{brand_id}/connections/gsc/properties")).json()["properties"]
    assert properties == ["https://other.example/", "https://another.example/"]

    assert (await client.post(f"/api/v1/brands/{brand_id}/connections/gsc/property", json={"property": "https://forged.example/"})).status_code == 422
    response = await client.post(f"/api/v1/brands/{brand_id}/connections/gsc/property", json={"property": "https://other.example/"})
    assert response.status_code == 200 and response.json()["status"] == "connected"
    rows = await _connection_rows(brand_id)
    assert decrypt_credentials(rows[0].credentials_encrypted)["site_url"] == "https://other.example/"


async def test_ga4_always_pends_then_pick_resolves(client, monkeypatch):
    _stub_google(monkeypatch, ga4_properties=[{"property_id": "123456", "display_name": "Main site"}])
    brand_id = await _signup(client)

    assert _result(await _callback(client, brand_id, "ga4")) == "pick_property"
    properties = (await client.get(f"/api/v1/brands/{brand_id}/connections/ga4/properties")).json()["properties"]
    assert properties == [{"property_id": "123456", "display_name": "Main site"}]

    response = await client.post(f"/api/v1/brands/{brand_id}/connections/ga4/property", json={"property": "123456"})
    assert response.status_code == 200 and response.json()["status"] == "connected"
    rows = await _connection_rows(brand_id)
    assert decrypt_credentials(rows[0].credentials_encrypted) == {"refresh_token": "rt-test", "property_id": "123456"}


async def test_callback_state_is_the_auth(client, monkeypatch):
    _stub_google(monkeypatch)
    brand_id = await _signup(client)

    tampered = await client.get("/api/v1/connections/google/callback", params={"state": "forged.deadbeef", "code": "x"})
    assert _result(tampered) == "error"

    expired_payload = base64.urlsafe_b64encode(json.dumps({"brand_id": brand_id, "provider": "gsc", "nonce": "n", "exp": int(time.time()) - 10}).encode())
    expired_signature = hmac.new(settings.TOKEN_ENCRYPTION_KEY.encode(), expired_payload, hashlib.sha256).hexdigest()
    expired = await client.get("/api/v1/connections/google/callback", params={"state": f"{expired_payload.decode()}.{expired_signature}", "code": "x"})
    assert _result(expired) == "expired"

    declined = await _callback(client, brand_id, "gsc", error="access_denied")
    assert _result(declined) == "denied"
    assert await _connection_rows(brand_id) == []


async def test_disconnect_revokes_and_forgets(client, monkeypatch):
    _stub_google(monkeypatch, gsc_properties=["sc-domain:example.com"])
    brand_id = await _signup(client)
    assert _result(await _callback(client, brand_id, "gsc")) == "connected"

    response = await client.delete(f"/api/v1/brands/{brand_id}/connections/gsc")
    assert response.status_code == 200 and response.json()["status"] == "disconnected"
    assert await _connection_rows(brand_id) == []
    assert (await client.get(f"/api/v1/brands/{brand_id}/connections")).json()["providers"] == []
