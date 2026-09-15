"""Seed the dev database: system markets + a test org/brand with history.

Re-run safe: each section checks whether it already ran and skips (step 0).

    docker compose run --rm --no-deps evergreen-backend python seed.py

What this sets up, step by step:
  1. system markets — the top 3 cities per state (+DC), ranked by
     population x county GDP. Three fetches, no per-city calls:
       a. Census ACS 5-year: population for every place  (CENSUS_API_KEY, .env)
       b. Census place-to-county relationship file: city -> county FIPS
       c. BEA CAGDP2: county GDP                         (BEA_API_KEY, .env)
     Without a BEA key/coverage, ranking degrades to population alone.
  2. a test organization
  3. one brand under it (Sunrise Dental)
  4. a PENDING site row (connect a real WP/Shopify sandbox via
     POST /brands/{id}/site to exercise audits + change application)
  5. two DONE visibility runs (last week + this week) -> the Brief gets a
     real score and a week-over-week delta
  6. two open findings (one fresh, one old) -> the Brief's site-health section
  7. two completed internal tasks this week -> the Brief's "work shipped"
  8. two OVERDUE client-contribution tasks (queued, backdated, NO queue job)
     -> what client_chase nudges about

Prints the brand id at the end — tinker.py uses it for the one-off runs."""

import asyncio
import os
import re
from datetime import datetime, timedelta

import httpx
import jwt
from sqlalchemy import select

import settings
from objects.brand import Brand
from objects.brand_market import BrandMarket
from objects.finding import SEVERITY_SERIOUS, SEVERITY_WARN, Finding
from objects.market import SOURCE_SYSTEM, Market
from objects.organization import Organization
from objects.site import Site
from objects.task import STATUS_DONE, STATUS_QUEUED, Task
from objects.user import User
from objects.visibility_run import STATUS_DONE as RUN_DONE
from objects.visibility_run import VisibilityRun
from utils.database import Database, ReplicaDatabase

##############################
# MARKETS (step 1)
##############################

CENSUS_API_KEY = os.environ.get("CENSUS_API_KEY", "")
BEA_API_KEY = os.environ.get("BEA_API_KEY", "")

# 2024 is the newest vintage BOTH sources publish (verified live 2026-08):
# ACS 5-year 2025 404s until ~Dec 2026, BEA county GDP 2025 lands late 2026 —
# bump this one constant when they do. The place-to-county file stays on the
# codes2020 vintage until the 2030 census; county assignments don't move.
CENSUS_YEAR = 2024
POPULATION_URL = f"https://api.census.gov/data/{CENSUS_YEAR}/acs/acs5"
PLACE_TO_COUNTY_URL = "https://www2.census.gov/geo/docs/reference/codes2020/national_place_by_county2020.txt"
COUNTY_GDP_URL = "https://apps.bea.gov/api/data/"

TOP_CITIES_PER_STATE = 3

# state FIPS -> USPS abbreviation, the 50 states + DC
STATE_ABBREV_BY_FIPS = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT", "10": "DE", "11": "DC",
    "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY",
    "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN", "28": "MS", "29": "MO", "30": "MT",
    "31": "NE", "32": "NV", "33": "NH", "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH",
    "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT",
    "50": "VT", "51": "VA", "53": "WA", "54": "WV", "55": "WI", "56": "WY",
}  # fmt: skip

# Census place names carry legal designations ("Los Angeles city",
# "Nashville-Davidson metropolitan government (balance)") — strip them.
PLACE_NAME_SUFFIXES = re.compile(r"\s+(city and borough|metropolitan government|metro government|unified government|consolidated government|urban county|city|town|village|borough|municipality|CDP)(\s*\(balance\))?$", re.IGNORECASE)


async def seed_system_markets(session) -> None:
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as web:
        print("1a. fetching place populations from Census ACS...")
        population_by_place_geoid = await fetch_place_populations(web)

        print("1b. fetching the place-to-county relationship file...")
        county_fips_by_place_geoid = await fetch_place_to_county_map(web)

        print("1c. fetching county GDP from BEA...")
        gdp_by_county_fips = await fetch_county_gdp(web)

    # rank: population x county GDP; population alone where GDP is unknown
    cities_by_state: dict[str, list[dict]] = {}
    for place_geoid, place in population_by_place_geoid.items():
        county_fips = county_fips_by_place_geoid.get(place_geoid)
        county_gdp = gdp_by_county_fips.get(county_fips or "")
        place["county_gdp"] = county_gdp
        place["ranking_score"] = place["population"] * county_gdp if county_gdp else float(place["population"])
        cities_by_state.setdefault(place["state"], []).append(place)

    markets_created = 0
    for state, cities in sorted(cities_by_state.items()):
        cities.sort(key=lambda city: city["ranking_score"], reverse=True)
        for winner in cities[:TOP_CITIES_PER_STATE]:
            session.add(Market(city=winner["city"], state=state, source=SOURCE_SYSTEM, population=winner["population"], county_gdp=int(winner["county_gdp"]) if winner["county_gdp"] else None, census_year=CENSUS_YEAR))
            markets_created += 1
    await session.flush()
    print(f"1.  seeded {markets_created} system markets (top {TOP_CITIES_PER_STATE} per state)")


async def fetch_place_populations(web: httpx.AsyncClient) -> dict[str, dict]:
    """Every incorporated place/CDP with its population, keyed by place GEOID."""
    if not CENSUS_API_KEY:
        raise SystemExit("CENSUS_API_KEY missing from .env — free instant key: https://api.census.gov/data/key_signup.html")
    response = await web.get(POPULATION_URL, params={"get": "NAME,B01003_001E", "for": "place:*", "in": "state:*", "key": CENSUS_API_KEY})
    response.raise_for_status()

    place_rows = response.json()[1:]  # first row is the column header
    places: dict[str, dict] = {}
    for name, population, state_fips, place_fips in place_rows:
        state = STATE_ABBREV_BY_FIPS.get(state_fips)
        if not state or not population or int(population) <= 0:
            continue
        city = PLACE_NAME_SUFFIXES.sub("", name.rsplit(",", 1)[0]).strip().rstrip(",")
        places[state_fips + place_fips] = {"city": city, "state": state, "population": int(population)}
    return places


async def fetch_place_to_county_map(web: httpx.AsyncClient) -> dict[str, str]:
    """Place GEOID -> county FIPS (SSCCC). A multi-county place keeps its
    first listed county — good enough for a ranking signal."""
    response = await web.get(PLACE_TO_COUNTY_URL)
    response.raise_for_status()

    lines = response.text.splitlines()
    header = lines[0].split("|")
    column_index_by_name = {column_name: index for index, column_name in enumerate(header)}
    required_columns = {"STATEFP", "PLACEFP", "COUNTYFP"}
    if not required_columns.issubset(column_index_by_name):
        raise SystemExit(f"place-to-county file header changed — expected {required_columns}, got: {header}")

    county_by_place: dict[str, str] = {}
    for line in lines[1:]:
        fields = line.split("|")
        place_geoid = fields[column_index_by_name["STATEFP"]] + fields[column_index_by_name["PLACEFP"]]
        if place_geoid not in county_by_place:  # first listed county wins
            county_by_place[place_geoid] = fields[column_index_by_name["STATEFP"]] + fields[column_index_by_name["COUNTYFP"]]
    return county_by_place


async def fetch_county_gdp(web: httpx.AsyncClient) -> dict[str, float]:
    """County FIPS -> all-industry GDP (BEA CAGDP2). Empty without a key —
    ranking then falls back to population alone."""
    if not BEA_API_KEY:
        print("    BEA_API_KEY missing — ranking by population only")
        return {}
    response = await web.get(COUNTY_GDP_URL, params={"UserID": BEA_API_KEY, "method": "GetData", "datasetname": "Regional", "TableName": "CAGDP2", "LineCode": "1", "GeoFips": "COUNTY", "Year": str(CENSUS_YEAR), "ResultFormat": "JSON"})
    response.raise_for_status()

    gdp_by_county: dict[str, float] = {}
    for row in response.json().get("BEAAPI", {}).get("Results", {}).get("Data", []):
        gdp_value = row.get("DataValue", "").replace(",", "")
        if gdp_value and gdp_value.replace(".", "").isdigit():
            gdp_by_county[row["GeoFips"]] = float(gdp_value)
    print(f"    GDP loaded for {len(gdp_by_county)} counties")
    return gdp_by_county


##############################
# TEST BRAND WORLD (steps 2-8)
##############################

TEST_ORGANIZATION_NAME = "Evergreen Test Org"
TEST_BRAND_NAME = "Evergreen"
TEST_BRAND_WEBSITE = "https://www.evergreenofficial.com"
TEST_BRAND_CITY, TEST_BRAND_STATE = "Mesa", "AZ"  # the local brand's ONE subscribed market

THIS_MONDAY = (datetime.now() - timedelta(days=datetime.now().weekday())).date()
LAST_MONDAY = THIS_MONDAY - timedelta(days=7)
THIS_WEEK_OVERALL_SCORE = 0.42
LAST_WEEK_OVERALL_SCORE = 0.35

CLIENT_TASK_BACKDATE_DAYS = 5  # older than CHASE_AFTER_DAYS (3) -> overdue


async def seed_test_brand_with_history(session) -> None:
    # ---- step 2: the organization -------------------------------------
    test_organization = Organization(name=TEST_ORGANIZATION_NAME)
    session.add(test_organization)
    await session.flush()
    print(f"2.  organization: {test_organization.name} ({test_organization.id})")

    # ---- step 3: the brand --------------------------------------------
    # Footprint is LOCAL on purpose. The .env carries a Census key, so step 1
    # also seeds ~154 system markets; a national brand's first fill would
    # collect active prompts x 154 cities x ACTIVE_ENGINES — a real bill for
    # a smoke test. A local brand collects only its subscribed brand_markets,
    # and a local brand with NO subscribed market collects nothing forever,
    # so subscribe exactly one city here: prompts x 1 x engines.
    test_brand = Brand(organization_id=test_organization.id, name=TEST_BRAND_NAME, website_url=TEST_BRAND_WEBSITE, footprint="local")
    session.add(test_brand)
    await session.flush()
    home_market = await Market.get_or_create_city(TEST_BRAND_CITY, TEST_BRAND_STATE)
    await BrandMarket.subscribe(test_brand.id, home_market.id)
    print(f"3.  brand: {test_brand.name} ({test_brand.id}) — local footprint, 1 market: {home_market.city}, {home_market.state}")

    # ---- step 4: a pending site ---------------------------------------
    # pending, NOT connected: a "connected" row with NULL credentials would
    # dead-letter change_set_apply on a raw InvalidToken the first time an
    # approved draft dispatches. Connect a real WP/Shopify sandbox through
    # POST /brands/{id}/site to exercise the apply path.
    pending_site = Site(brand_id=test_brand.id, platform="wordpress")
    session.add(pending_site)
    await session.flush()
    print(f"4.  pending site: {pending_site.platform} ({pending_site.id}) — connect a real one via POST /brands/{{id}}/site")

    # # ---- step 5: two DONE visibility runs (the Brief's trend) ---------
    # last_week_run = VisibilityRun(brand_id=test_brand.id, week_of=LAST_MONDAY, status=RUN_DONE, overall_score=LAST_WEEK_OVERALL_SCORE, detection_rate=0.40, share_of_voice=0.22, positive_rate=0.30, top3_rate=0.25, citation_rate=0.10)
    # this_week_run = VisibilityRun(brand_id=test_brand.id, week_of=THIS_MONDAY, status=RUN_DONE, overall_score=THIS_WEEK_OVERALL_SCORE, detection_rate=0.50, share_of_voice=0.28, positive_rate=0.35, top3_rate=0.30, citation_rate=0.12)
    # session.add(last_week_run)
    # session.add(this_week_run)
    # await session.flush()
    # print(f"5.  visibility runs: {LAST_WEEK_OVERALL_SCORE} (last week) -> {THIS_WEEK_OVERALL_SCORE} (this week)")

    # # ---- step 6: open findings (the Brief's site-health section) ------
    # fresh_finding = Finding(brand_id=test_brand.id, site_id=pending_site.id, kind="missing_schema", severity=SEVERITY_WARN, summary="Page has no structured data (ld+json): /services")
    # old_finding = Finding(brand_id=test_brand.id, site_id=pending_site.id, kind="blocked_ai_crawler", severity=SEVERITY_SERIOUS, summary="robots.txt blocks GPTBot — invisible to that AI engine", created_at=datetime.now() - timedelta(days=20))
    # session.add(fresh_finding)
    # session.add(old_finding)
    # await session.flush()
    # print("6.  findings: 1 fresh (missing_schema), 1 old (blocked_ai_crawler)")

    # # ---- step 7: internal work completed this week --------------------
    # session.add(Task(brand_id=test_brand.id, name="site_audit", status=STATUS_DONE, finished_at=datetime.now() - timedelta(days=1)))
    # session.add(Task(brand_id=test_brand.id, name="visibility_run", status=STATUS_DONE, finished_at=datetime.now() - timedelta(days=1)))
    # await session.flush()
    # print("7.  completed this week: site_audit, visibility_run")

    # # ---- step 8: overdue client-contribution tasks --------------------
    # # Inserted directly (NOT via create_task): client tasks are rows that
    # # never get a queue job — exactly what client_chase scans for.
    # client_task_backdate = datetime.now() - timedelta(days=CLIENT_TASK_BACKDATE_DAYS)
    # session.add(Task(brand_id=test_brand.id, name="grant_access", status=STATUS_QUEUED, created_at=client_task_backdate))
    # session.add(Task(brand_id=test_brand.id, name="upload_photos", status=STATUS_QUEUED, created_at=client_task_backdate))
    # await session.flush()
    # print(f"8.  overdue client tasks: grant_access, upload_photos (waiting {CLIENT_TASK_BACKDATE_DAYS} days)")

    # print(f"\nseeded. brand_id for tinker.py: {test_brand.id}")


async def seed_test_user(session, organization_id: str) -> None:
    """Tie the TEST_RUNNER Supabase account to the test organization so a
    bearer token from that account passes utils.authz.guard as an admin of
    the seeded org (GET /me -> onboarded brand, every /brands/{id} route
    resolves). guard looks users up by the token's `sub`, so we must learn
    that id the only way there is: sign the runner in (same call the test
    suite's tests/_auth makes). Runs on EVERY seed — the suite's
    release_runner() detaches the runner between signups, so re-tying is
    what a re-run wants. Skips cleanly when the creds aren't in .env."""
    url = str(settings.SUPABASE_URL).strip().rstrip("/")
    anon_key = str(getattr(settings, "SUPABASE_PUBLISHABLE_KEY", "")).strip()
    email = str(getattr(settings, "TEST_RUNNER_EMAIL", "")).strip().lower()
    password = str(getattr(settings, "TEST_RUNNER_PASSWORD", ""))
    if not url or url == "https://test.invalid" or not anon_key or not email or not password:
        print("9.  test user: SUPABASE_URL / SUPABASE_PUBLISHABLE_KEY / TEST_RUNNER_EMAIL / TEST_RUNNER_PASSWORD not set — skipping (no bearer login for the seeded org)")
        return
    async with httpx.AsyncClient(timeout=15.0) as web:
        response = await web.post(f"{url}/auth/v1/token", params={"grant_type": "password"}, headers={"apikey": anon_key, "Content-Type": "application/json"}, json={"email": email, "password": password})
    if response.status_code != 200:
        print(f"9.  test user: Supabase sign-in failed for {email} ({response.status_code}) — skipping")
        return
    # decoded WITHOUT verification only to learn the id; the app verifies for real on every request
    supabase_user_id = jwt.decode(response.json()["access_token"], options={"verify_signature": False})["sub"]
    user = await User.upsert_from_claims(auth_sub=supabase_user_id, email=email)
    await User.claim_organization(user.id, organization_id)
    print(f"9.  test user: {email} ({user.id}) -> admin of organization {organization_id}")


##############################
# MAIN
##############################


async def seed() -> None:
    async with ReplicaDatabase().connection():
        async with Database().connection():
            session = Database.session

            # ---- step 0: re-run safety — each section skips if it already ran
            markets_already_seeded = (await session.execute(select(Market.id).where(Market.source == SOURCE_SYSTEM).limit(1))).first() is not None
            organization_already_seeded = (await session.execute(select(Organization.id).where(Organization.name == TEST_ORGANIZATION_NAME).limit(1))).first() is not None

            if markets_already_seeded:
                print("1.  system markets already seeded — skipping")
            else:
                await seed_system_markets(session)

            if organization_already_seeded:
                print(f"2-8. '{TEST_ORGANIZATION_NAME}' already exists — skipping")
            else:
                await seed_test_brand_with_history(session)

            # ---- step 9: the login — every run, so the runner is always tied to THIS org
            test_organization_id = (await session.execute(select(Organization.id).where(Organization.name == TEST_ORGANIZATION_NAME).limit(1))).scalar_one()
            await seed_test_user(session, test_organization_id)

            await session.commit()


if __name__ == "__main__":
    asyncio.run(seed())
