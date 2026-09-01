"""Local footprints: signup(footprint=local, cities) creates shared market
rows (one per physical city — the corpus dedup invariant, case-insensitive,
constraint-backed) plus brand_markets subscriptions, and Market.by_brand
serves exactly those cities to collection. City names are uniqued per run
("Test City <hex>") so reruns never collide with the seeded system markets.
Runs in compose (make test), needs the database."""

import uuid

from sqlalchemy import func, select

from objects.brand_market import BrandMarket
from objects.market import SOURCE_LOCAL, SOURCE_SYSTEM, Market
from utils.database import Database, ReplicaDatabase


def _city() -> str:
    return f"Test City {uuid.uuid4().hex[:8]}"


async def _signup(client, **overrides):
    payload = {"organization_name": f"test-org-{uuid.uuid4()}", "brand_name": f"test brand {uuid.uuid4()}", "website_url": "https://example.com", **overrides}
    return await client.post("/api/v1/onboarding/signup", json=payload)


async def _markets_named(city: str) -> list[Market]:
    rows = await ReplicaDatabase.session.execute(select(Market).where(func.lower(Market.city) == city.lower()))
    return list(rows.scalars().all())


async def test_local_signup_creates_markets_and_subscriptions(client):
    city_a, city_b = _city(), _city()
    # duplicate spellings of city_a dedupe inside the payload
    response = await _signup(client, footprint="local", cities=[f"{city_a}, AZ", f"  {city_a.lower()} ,  az ", f"{city_b}, AZ"])
    assert response.status_code == 200
    brand_id = response.json()["brand_id"]

    async with ReplicaDatabase().connection(), Database().connection():
        rows_a, rows_b = await _markets_named(city_a), await _markets_named(city_b)
        assert len(rows_a) == 1 and rows_a[0].source == SOURCE_LOCAL and rows_a[0].city == city_a  # first-typed casing kept
        assert len(rows_b) == 1
        subscriptions = list((await ReplicaDatabase.session.execute(select(BrandMarket).where(BrandMarket.brand_id == brand_id))).scalars().all())
        assert {subscription.market_id for subscription in subscriptions} == {rows_a[0].id, rows_b[0].id}
        assert all(subscription.source == "user" for subscription in subscriptions)
        # what collection will see
        assert {market.city for market in await Market.by_brand(brand_id, "local")} == {city_a, city_b}


async def test_existing_market_is_reused_not_duplicated(client):
    city = _city()
    assert (await _signup(client, footprint="local", cities=[f"{city}, TX"])).status_code == 200
    assert (await _signup(client, footprint="local", cities=[f"{city.upper()}, tx"])).status_code == 200

    async with ReplicaDatabase().connection(), Database().connection():
        rows = await _markets_named(city)
        assert len(rows) == 1  # second signup subscribed to the SAME shared row
        subscriber_count = (await ReplicaDatabase.session.execute(select(func.count()).select_from(BrandMarket).where(BrandMarket.market_id == rows[0].id))).scalar_one()
        assert subscriber_count == 2


async def test_system_market_is_reused_by_local_signup(client):
    city = _city()
    async with ReplicaDatabase().connection(), Database().connection():
        Database.session.add(Market(city=city, state="WA", source=SOURCE_SYSTEM))
        await Database.session.commit()

    assert (await _signup(client, footprint="local", cities=[f"{city}, WA"])).status_code == 200
    async with ReplicaDatabase().connection(), Database().connection():
        rows = await _markets_named(city)
        assert len(rows) == 1 and rows[0].source == SOURCE_SYSTEM  # untouched, just subscribed


async def test_local_signup_validation(client):
    assert (await _signup(client, footprint="local")).status_code == 422  # no cities = silent-collect-nothing, made loud
    assert (await _signup(client, footprint="local", cities=[])).status_code == 422
    assert (await _signup(client, footprint="local", cities=["Mesa AZ"])).status_code == 422  # missing comma
    assert (await _signup(client, footprint="local", cities=["Mesa, ZZ"])).status_code == 422  # not a state
    assert (await _signup(client, footprint="local", cities=[f"{_city()}, AZ" for _ in range(26)])).status_code == 422  # over the cap
    # national signups ignore the cities field entirely
    assert (await _signup(client, footprint="national", cities=["not even a city"])).status_code == 200


async def test_market_typeahead(client):
    city = _city()
    assert (await _signup(client, footprint="local", cities=[f"{city}, ID"])).status_code == 200

    matches = (await client.get("/api/v1/markets", params={"q": city[:14]})).json()["markets"]
    assert {"city": city, "state": "ID"} in matches
    assert (await client.get("/api/v1/markets", params={"q": ""})).json()["markets"] == []
