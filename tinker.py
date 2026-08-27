"""We insert the Task row manually and call the runner's .func directly so this
script is the ONLY executor: create_task() would also enqueue a queue job,
and the live worker container would pick that up and run the brief a second
time (double Anthropic spend)."""

import asyncio

from sqlalchemy import select

from objects.brand import Brand
from objects.task import Task
from seed import TEST_BRAND_NAME, seed
from utils.database import Database, ReplicaDatabase
from worker.tasks.briefs import weekly_brief


async def tinker() -> None:
    await seed()

    # ---- step 2: one-off weekly_brief ---------------------------------
    async with ReplicaDatabase().connection():
        async with Database().connection():
            session = Database.session
            brand = (await session.execute(select(Brand).where(Brand.name == TEST_BRAND_NAME))).scalars().first()
            # bare row, deliberately not create_task() — see the docstring up top
            brief_task = Task(brand_id=brand.id, name="weekly_brief")
            session.add(brief_task)
            await session.commit()

    await weekly_brief.func(task_id=brief_task.id, brand_id=brand.id)


if __name__ == "__main__":
    asyncio.run(tinker())
