"""PATCH /brands/{id} — the whitelist the interview extraction's
"client-confirmed" half rides. Only schema-carried fields apply; anything
else (platform, footprint, onboarded_at) is a loud 422 via extra="forbid".
Runs in compose (make test), needs the database."""

import uuid


async def _signup(client):
    payload = {"organization_name": f"test-org-{uuid.uuid4()}", "brand_name": f"test brand {uuid.uuid4()}", "website_url": "https://example.com", "footprint": "national"}
    response = await client.post("/api/v1/onboarding/signup", json=payload)
    assert response.status_code == 200
    return response.json()["brand_id"]


async def test_patch_applies_only_the_sent_fields(client):
    brand_id = await _signup(client)

    response = await client.patch(f"/api/v1/brands/{brand_id}", json={"description": "Sells nail polish online.", "category": "Beauty", "audience": "Online shoppers", "differentiators": ["Natural ingredients", "  ", "High quality "]})
    assert response.status_code == 200
    body = response.json()
    assert body["description"] == "Sells nail polish online."
    assert body["differentiators"] == ["Natural ingredients", "High quality"]  # blanks dropped, whitespace trimmed
    assert body["footprint"] == "national"  # untouched

    # a later partial patch leaves the earlier fields alone
    second = await client.patch(f"/api/v1/brands/{brand_id}", json={"category": "Beauty & cosmetics"})
    assert second.status_code == 200
    assert second.json()["category"] == "Beauty & cosmetics"
    assert second.json()["description"] == "Sells nail polish online."

    # the read layer serves the confirmed facts
    brands = (await client.get("/api/v1/brands")).json()["brands"]
    row = next(brand for brand in brands if brand["id"] == brand_id)
    assert row["category"] == "Beauty & cosmetics" and row["differentiators"] == ["Natural ingredients", "High quality"]
    overview_brand = (await client.get(f"/api/v1/brands/{brand_id}/overview")).json()["brand"]
    assert overview_brand["description"] == "Sells nail polish online." and overview_brand["platform"] == "unknown"


async def test_patch_validates_name_and_url(client):
    brand_id = await _signup(client)

    assert (await client.patch(f"/api/v1/brands/{brand_id}", json={"name": "   "})).status_code == 422
    assert (await client.patch(f"/api/v1/brands/{brand_id}", json={"website_url": ""})).status_code == 422

    response = await client.patch(f"/api/v1/brands/{brand_id}", json={"name": "MADR", "website_url": "lippmannofficial.com"})
    assert response.status_code == 200
    assert response.json()["name"] == "MADR"
    assert response.json()["website_url"] == "https://lippmannofficial.com"


async def test_patch_rejects_fields_outside_the_whitelist(client):
    brand_id = await _signup(client)
    for payload in ({"footprint": "local"}, {"platform": "wordpress"}, {"onboarded_at": None}, {"organization_id": "x"}):
        response = await client.patch(f"/api/v1/brands/{brand_id}", json=payload)
        assert response.status_code == 422, payload

    # an empty patch is a valid no-op returning current state
    assert (await client.patch(f"/api/v1/brands/{brand_id}", json={})).status_code == 200
