"""Site connect: credentials are verified by a LIVE adapter read before
anything is stored (nothing persists on a failed verify; a failed RE-connect
never clobbers a working connection), a successful connect closes the
grant_access chase task and fires the connect-half site audit, and the brand
has exactly ONE site row, upserted. Adapter verify() is monkeypatched — no
network calls. Runs in compose (make test), needs the database."""

import uuid

from sqlalchemy import select

from adapters.cms.common import CmsWriteError
from adapters.cms.shopify_adapter import ShopifyAdapter
from adapters.cms.wordpress_adapter import WordPressAdapter
from objects.site import Site
from tests._auth import release_runner
from utils.database import Database, ReplicaDatabase
from utils.token_crypto import decrypt_credentials

WP_CREDENTIALS = {"base_url": "sunrisedental.example", "username": "astra", "app_password": "abcd efgh"}
SHOPIFY_CREDENTIALS = {"shop_domain": "https://sunrise.myshopify.com/", "client_id": "test-client-id", "client_secret": "test-client-secret"}


def _stub_verify(monkeypatch, adapter_class, error: str | None = None):
    async def verify(self):
        if error:
            raise CmsWriteError(error)

    monkeypatch.setattr(adapter_class, "verify", verify)


async def _signup_and_launch(client):
    await release_runner()  # one org per user — free the runner so this signup claims fresh
    payload = {"organization_name": f"test-org-{uuid.uuid4()}", "brand_name": f"test brand {uuid.uuid4()}", "website_url": "https://example.com", "footprint": "national"}
    brand_id = (await client.post("/api/v1/onboarding/signup", json=payload)).json()["brand_id"]
    assert (await client.post(f"/api/v1/brands/{brand_id}/launch")).status_code == 200
    return brand_id


async def _site_rows(brand_id: str) -> list[Site]:
    async with ReplicaDatabase().connection(), Database().connection():
        rows = await ReplicaDatabase.session.execute(select(Site).where(Site.brand_id == brand_id))
        return list(rows.scalars().all())


async def test_connect_verifies_stores_and_unblocks(client, monkeypatch):
    _stub_verify(monkeypatch, WordPressAdapter)
    brand_id = await _signup_and_launch(client)

    response = await client.post(f"/api/v1/brands/{brand_id}/site", json={"platform": "wordpress", "credentials": WP_CREDENTIALS})
    assert response.status_code == 200
    assert response.json()["status"] == "connected" and response.json()["connected_at"]

    card = (await client.get(f"/api/v1/brands/{brand_id}/connections")).json()
    assert card["site"]["platform"] == "wordpress" and card["site"]["status"] == "connected"
    assert card["providers"] == []

    # brand.platform follows the connected site
    brands = (await client.get("/api/v1/brands")).json()["brands"]
    assert next(brand for brand in brands if brand["id"] == brand_id)["platform"] == "wordpress"

    # grant_access is delivered — the chase lane no longer shows it
    approvals = (await client.get(f"/api/v1/brands/{brand_id}/approvals")).json()
    assert "grant_access" not in {task["name"] for task in approvals["waiting_on_you"]}

    # launch queued one audit; connect queued the second
    worklog = (await client.get(f"/api/v1/brands/{brand_id}/worklog")).json()
    assert len([task for task in worklog["queued"] if task["name"] == "site_audit"]) == 2

    # stored credentials survive an encrypt/decrypt round-trip with the url normalized
    rows = await _site_rows(brand_id)
    assert len(rows) == 1
    assert decrypt_credentials(rows[0].credentials_encrypted)["base_url"] == "https://sunrisedental.example"


async def test_failed_verify_stores_no_credentials(client, monkeypatch):
    _stub_verify(monkeypatch, WordPressAdapter, error="401: bad application password")
    brand_id = await _signup_and_launch(client)

    response = await client.post(f"/api/v1/brands/{brand_id}/site", json={"platform": "wordpress", "credentials": WP_CREDENTIALS})
    assert response.status_code == 422 and "bad application password" in response.json()["detail"]

    card = (await client.get(f"/api/v1/brands/{brand_id}/connections")).json()
    assert card["site"]["status"] == "error"
    rows = await _site_rows(brand_id)
    assert len(rows) == 1 and rows[0].credentials_encrypted is None

    # grant_access still waits — nothing was actually granted
    approvals = (await client.get(f"/api/v1/brands/{brand_id}/approvals")).json()
    assert "grant_access" in {task["name"] for task in approvals["waiting_on_you"]}


async def test_failed_reconnect_keeps_the_working_connection(client, monkeypatch):
    _stub_verify(monkeypatch, WordPressAdapter)
    brand_id = await _signup_and_launch(client)
    assert (await client.post(f"/api/v1/brands/{brand_id}/site", json={"platform": "wordpress", "credentials": WP_CREDENTIALS})).status_code == 200

    _stub_verify(monkeypatch, WordPressAdapter, error="401")
    assert (await client.post(f"/api/v1/brands/{brand_id}/site", json={"platform": "wordpress", "credentials": {**WP_CREDENTIALS, "app_password": "typo"}})).status_code == 422

    card = (await client.get(f"/api/v1/brands/{brand_id}/connections")).json()
    assert card["site"]["status"] == "connected"
    rows = await _site_rows(brand_id)
    assert len(rows) == 1 and rows[0].credentials_encrypted is not None


async def test_reconnect_upserts_one_row_and_disconnect_clears_it(client, monkeypatch):
    _stub_verify(monkeypatch, WordPressAdapter)
    _stub_verify(monkeypatch, ShopifyAdapter)
    brand_id = await _signup_and_launch(client)

    assert (await client.post(f"/api/v1/brands/{brand_id}/site", json={"platform": "wordpress", "credentials": WP_CREDENTIALS})).status_code == 200
    assert (await client.post(f"/api/v1/brands/{brand_id}/site", json={"platform": "shopify", "credentials": SHOPIFY_CREDENTIALS})).status_code == 200

    rows = await _site_rows(brand_id)
    assert len(rows) == 1 and rows[0].platform == "shopify"
    assert decrypt_credentials(rows[0].credentials_encrypted)["shop_domain"] == "sunrise.myshopify.com"  # scheme + slash stripped

    response = await client.delete(f"/api/v1/brands/{brand_id}/site")
    assert response.status_code == 200 and response.json()["status"] == "disconnected"
    rows = await _site_rows(brand_id)
    assert len(rows) == 1 and rows[0].credentials_encrypted is None


async def test_connect_validation(client):
    brand_id = await _signup_and_launch(client)
    assert (await client.post(f"/api/v1/brands/{brand_id}/site", json={"platform": "wix", "credentials": {}})).status_code == 422
    response = await client.post(f"/api/v1/brands/{brand_id}/site", json={"platform": "wordpress", "credentials": {"base_url": "x.example"}})
    assert response.status_code == 422 and "app_password" in response.json()["detail"]
    # nothing to disconnect on a brand that never connected
    await release_runner()
    fresh_payload = {"organization_name": f"test-org-{uuid.uuid4()}", "brand_name": f"test brand {uuid.uuid4()}", "website_url": "https://example.com"}
    fresh_brand_id = (await client.post("/api/v1/onboarding/signup", json=fresh_payload)).json()["brand_id"]
    assert (await client.delete(f"/api/v1/brands/{fresh_brand_id}/site")).status_code == 404
