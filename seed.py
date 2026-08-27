"""Seed a test org + brand with enough history to exercise the AM tasks.

Fresh-DB friendly and re-run safe: aborts if the test org already exists.

    docker compose run --rm --no-deps astra-backend python seed.py

What this sets up, step by step:
  1. a test organization
  2. one brand under it (Sunrise Dental)
  3. a connected site row (so audit-style tasks have a target)
  4. two DONE visibility runs (last week + this week) -> the Brief gets a
     real score and a week-over-week delta
  5. two open findings (one fresh, one old) -> the Brief's site-health section
  6. two completed internal tasks this week -> the Brief's "work shipped"
  7. two OVERDUE client-contribution tasks (queued, backdated, NO queue job)
     -> what client_chase nudges about

Prints the brand id at the end — tinker.py uses it for the one-off runs."""

import asyncio
from datetime import datetime, timedelta

from sqlalchemy import select

from objects.brand import Brand
from objects.finding import SEVERITY_SERIOUS, SEVERITY_WARN, Finding
from objects.organization import Organization
from objects.site import STATUS_CONNECTED, Site
from objects.task import STATUS_DONE, STATUS_QUEUED, Task
from objects.visibility_run import STATUS_DONE as RUN_DONE
from objects.visibility_run import VisibilityRun
from utils.database import Database, ReplicaDatabase

TEST_ORGANIZATION_NAME = "Astra Test Org"
TEST_BRAND_NAME = "Sunrise Dental"
TEST_BRAND_WEBSITE = "https://www.sunrisedental.example"

THIS_MONDAY = (datetime.now() - timedelta(days=datetime.now().weekday())).date()
LAST_MONDAY = THIS_MONDAY - timedelta(days=7)

THIS_WEEK_OVERALL_SCORE = 0.42
LAST_WEEK_OVERALL_SCORE = 0.35

CLIENT_TASK_BACKDATE_DAYS = 5  # older than CHASE_AFTER_DAYS (3) -> overdue


async def seed() -> None:
    async with ReplicaDatabase().connection():
        async with Database().connection():
            session = Database.session

            # ---- step 0: refuse to double-seed --------------------------------
            existing = await session.execute(select(Organization).where(Organization.name == TEST_ORGANIZATION_NAME))
            if existing.scalars().first():
                print(f"'{TEST_ORGANIZATION_NAME}' already exists — nothing seeded. Drop/recreate the DB to reseed.")
                return

            # ---- step 1: the organization -------------------------------------
            test_organization = Organization(name=TEST_ORGANIZATION_NAME)
            session.add(test_organization)
            await session.flush()
            print(f"1. organization: {test_organization.name} ({test_organization.id})")

            # ---- step 2: the brand --------------------------------------------
            dental_brand = Brand(
                organization_id=test_organization.id,
                name=TEST_BRAND_NAME,
                website_url=TEST_BRAND_WEBSITE,
                footprint="national",
            )
            session.add(dental_brand)
            await session.flush()
            print(f"2. brand: {dental_brand.name} ({dental_brand.id})")

            # ---- step 3: a connected site -------------------------------------
            connected_site = Site(brand_id=dental_brand.id, platform="wordpress", status=STATUS_CONNECTED, connected_at=datetime.now())
            session.add(connected_site)
            await session.flush()
            print(f"3. connected site: {connected_site.platform} ({connected_site.id})")

            # ---- step 4: two DONE visibility runs (the Brief's trend) ---------
            last_week_run = VisibilityRun(
                brand_id=dental_brand.id,
                week_of=LAST_MONDAY,
                status=RUN_DONE,
                overall_score=LAST_WEEK_OVERALL_SCORE,
                detection_rate=0.40,
                share_of_voice=0.22,
                positive_rate=0.30,
                top3_rate=0.25,
                citation_rate=0.10,
            )
            this_week_run = VisibilityRun(
                brand_id=dental_brand.id,
                week_of=THIS_MONDAY,
                status=RUN_DONE,
                overall_score=THIS_WEEK_OVERALL_SCORE,
                detection_rate=0.50,
                share_of_voice=0.28,
                positive_rate=0.35,
                top3_rate=0.30,
                citation_rate=0.12,
            )
            session.add(last_week_run)
            session.add(this_week_run)
            await session.flush()
            print(f"4. visibility runs: {LAST_WEEK_OVERALL_SCORE} (last week) -> {THIS_WEEK_OVERALL_SCORE} (this week)")

            # ---- step 5: open findings (the Brief's site-health section) ------
            fresh_finding = Finding(
                brand_id=dental_brand.id,
                site_id=connected_site.id,
                kind="missing_schema",
                severity=SEVERITY_WARN,
                summary="Page has no structured data (ld+json): /services",
            )
            old_finding = Finding(
                brand_id=dental_brand.id,
                site_id=connected_site.id,
                kind="blocked_ai_crawler",
                severity=SEVERITY_SERIOUS,
                summary="robots.txt blocks GPTBot — invisible to that AI engine",
                created_at=datetime.now() - timedelta(days=20),
            )
            session.add(fresh_finding)
            session.add(old_finding)
            await session.flush()
            print("5. findings: 1 fresh (missing_schema), 1 old (blocked_ai_crawler)")

            # ---- step 6: internal work completed this week --------------------
            completed_audit_task = Task(brand_id=dental_brand.id, name="site_audit", status=STATUS_DONE, finished_at=datetime.now() - timedelta(days=1))
            completed_visibility_task = Task(brand_id=dental_brand.id, name="visibility_run", status=STATUS_DONE, finished_at=datetime.now() - timedelta(days=1))
            session.add(completed_audit_task)
            session.add(completed_visibility_task)
            await session.flush()
            print("6. completed this week: site_audit, visibility_run")

            # ---- step 7: overdue client-contribution tasks --------------------
            # Inserted directly (NOT via create_task): client tasks are rows
            # that never get a queue job — exactly what client_chase scans for.
            client_task_backdate = datetime.now() - timedelta(days=CLIENT_TASK_BACKDATE_DAYS)
            overdue_access_task = Task(brand_id=dental_brand.id, name="grant_access", status=STATUS_QUEUED, created_at=client_task_backdate)
            overdue_photos_task = Task(brand_id=dental_brand.id, name="upload_photos", status=STATUS_QUEUED, created_at=client_task_backdate)
            session.add(overdue_access_task)
            session.add(overdue_photos_task)
            await session.flush()
            print(f"7. overdue client tasks: grant_access, upload_photos (waiting {CLIENT_TASK_BACKDATE_DAYS} days)")

            await session.commit()
            print(f"\nseeded. brand_id for tinker.py: {dental_brand.id}")


if __name__ == "__main__":
    asyncio.run(seed())
