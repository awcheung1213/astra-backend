"""The auth port (geoviz pattern): no token = 401 everywhere guarded; a
valid token JIT-provisions the users row; the guard 403s any resource owned
by someone else's org; signup claims exactly one org per user (re-entry with
an unlaunched brand hands the ids back; a launched brand 409s). The foreign
org is built directly in the DB — no second Supabase account needed."""

import uuid

from objects.brand import Brand
from objects.organization import Organization
from objects.user import User
from tests._auth import release_runner, runner_sub
from utils.database import Database, ReplicaDatabase


async def _signup(client, **overrides):
    await release_runner()
    payload = {"organization_name": f"test-org-{uuid.uuid4()}", "brand_name": f"test brand {uuid.uuid4()}", "website_url": "https://example.com", "footprint": "national", **overrides}
    response = await client.post("/api/v1/onboarding/signup", json=payload)
    assert response.status_code == 200
    return response.json()


async def _foreign_brand() -> str:
    """A brand in an org the runner does NOT belong to."""
    async with ReplicaDatabase().connection(), Database().connection():
        organization = Organization(name=f"test-org-{uuid.uuid4()}")
        Database.session.add(organization)
        await Database.session.flush()
        brand = Brand(organization_id=organization.id, name=f"test brand {uuid.uuid4()}", website_url="https://example.com")
        Database.session.add(brand)
        await Database.session.commit()
        return brand.id


async def test_no_token_is_401(anonymous_client):
    for path in ("/api/v1/me", "/api/v1/brands", "/api/v1/onboarding/signup"):
        response = await anonymous_client.get(path) if path != "/api/v1/onboarding/signup" else await anonymous_client.post(path, json={})
        assert response.status_code == 401, path


async def test_me_jit_provisions_and_gates_onboarding(client):
    await release_runner()
    me = (await client.get("/api/v1/me")).json()
    assert me["organization_id"] is None and me["onboarded"] is False
    async with ReplicaDatabase().connection(), Database().connection():
        assert await User.get_by_sub(runner_sub()) is not None  # the row exists — JIT

    signup = await _signup(client)
    me = (await client.get("/api/v1/me")).json()
    assert me["organization_id"] == signup["organization_id"]
    assert me["brand_id"] == signup["brand_id"] and me["onboarded"] is False  # signed up, not launched

    assert (await client.post(f"/api/v1/brands/{signup['brand_id']}/launch", json={})).status_code == 200
    assert (await client.get("/api/v1/me")).json()["onboarded"] is True


async def test_foreign_brand_is_403_and_brands_list_is_scoped(client):
    mine = await _signup(client)
    foreign_brand_id = await _foreign_brand()

    assert (await client.get(f"/api/v1/brands/{foreign_brand_id}/overview")).status_code == 403
    assert (await client.patch(f"/api/v1/brands/{foreign_brand_id}", json={"name": "hijack"})).status_code == 403
    assert (await client.post(f"/api/v1/brands/{foreign_brand_id}/launch", json={})).status_code == 403
    # an id that exists nowhere is a 404, not a 403 leak-probe answer
    assert (await client.get(f"/api/v1/brands/{uuid.uuid4()}/overview")).status_code == 404

    listed = (await client.get("/api/v1/brands")).json()["brands"]
    assert {brand["id"] for brand in listed} == {mine["brand_id"]}


async def test_signup_claims_once_reenters_unlaunched_409s_launched(client):
    first = await _signup(client)

    # same user, second signup, brand not launched -> the wizard re-entry
    # path hands back the existing ids instead of creating or 409ing
    payload = {"organization_name": f"test-org-{uuid.uuid4()}", "brand_name": f"test brand {uuid.uuid4()}", "website_url": "https://example.com"}
    reentry = await client.post("/api/v1/onboarding/signup", json=payload)
    assert reentry.status_code == 200 and reentry.json() == {"organization_id": first["organization_id"], "brand_id": first["brand_id"]}

    # launched -> the caller shouldn't be in the wizard at all
    assert (await client.post(f"/api/v1/brands/{first['brand_id']}/launch", json={})).status_code == 200
    assert (await client.post("/api/v1/onboarding/signup", json=payload)).status_code == 409
