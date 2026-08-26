"""The transactional-enqueue guarantee: tasks row + procrastinate job land in
ONE commit — or neither does. Runs in compose (make test), needs the database."""

import uuid

from sqlalchemy import text

from objects.brand import Brand
from objects.organization import Organization
from utils.database import Database, ReplicaDatabase
from utils.task_queue import create_task


async def _job_count(session, task_id: str) -> int:
    rows = await session.execute(text("SELECT count(*) FROM procrastinate_jobs WHERE args->>'task_id' = :tid"), {"tid": task_id})
    return rows.scalar_one()


async def _task_count(session, task_id: str) -> int:
    rows = await session.execute(text("SELECT count(*) FROM tasks WHERE id = :tid"), {"tid": task_id})
    return rows.scalar_one()


async def _make_brand(session) -> str:
    organization = Organization(name=f"test-org-{uuid.uuid4()}")
    session.add(organization)
    await session.flush()
    brand = Brand(organization_id=organization.id, name="test brand", website_url="https://example.com")
    session.add(brand)
    await session.flush()
    return brand.id


async def test_create_task_commits_row_and_job_together():
    async with ReplicaDatabase().connection(), Database().connection():
        session = Database.session
        brand_id = await _make_brand(session)
        task = await create_task(brand_id=brand_id, name="site_audit")
        await session.commit()

        assert await _task_count(session, task.id) == 1
        assert await _job_count(session, task.id) == 1


async def test_create_task_rolls_back_row_and_job_together():
    async with ReplicaDatabase().connection(), Database().connection():
        session = Database.session
        brand_id = await _make_brand(session)
        task = await create_task(brand_id=brand_id, name="site_audit")
        await session.rollback()

        assert await _task_count(session, task.id) == 0
        assert await _job_count(session, task.id) == 0
