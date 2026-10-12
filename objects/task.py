import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, func, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from exceptions.exceptions import TaskNotFound
from objects.base_object import BaseObject
from utils import clock
from utils.database import Database, ReplicaDatabase

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_DEAD = "dead"  # dead-letter: exceeded max_attempts — surfaced to the operator


class Task(SQLModel, BaseObject, table=True):
    """Queue contract (design doc): claim-and-lease, never lock-while-working;
    token budgets span the task's LIFETIME (across retries), not one attempt."""

    __tablename__ = "tasks"
    __table_args__ = (
        # partial index on live statuses — claims never scan the completed pile
        Index("ix_tasks_live", "name", "priority", "created_at", postgresql_where=text("status IN ('queued', 'running')")),
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    brand_id: str = Field(sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True))
    name: str
    status: str = Field(default=STATUS_QUEUED, sa_column=Column(String, server_default=STATUS_QUEUED, nullable=False))
    priority: int = Field(default=0, sa_column=Column(Integer, server_default="0", nullable=False))
    payload: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="{}"))
    token_budget: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    tokens_spent: int = Field(default=0, sa_column=Column(Integer, server_default="0", nullable=False))
    attempt_count: int = Field(default=0, sa_column=Column(Integer, server_default="0", nullable=False))
    max_attempts: int = Field(default=3, sa_column=Column(Integer, server_default="3", nullable=False))
    # no claimed_by/lease columns: claim, lease, and stalled-worker recovery are
    # procrastinate's job (heartbeats) — this row tracks MEANING, not dispatch
    finished_at: datetime | None = Field(default=None, sa_column=Column(DateTime, nullable=True))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, default=clock.column_default(), server_default=func.now(), nullable=False))

    @staticmethod
    async def get(id: str) -> "Task":
        task = await Database.session.get(Task, id)
        if task is None:
            raise TaskNotFound()
        return task

    @staticmethod
    async def first_queued(brand_id: str, name: str) -> "Task | None":
        # oldest first: a hand-run or a runner draining a queue must take the
        # rows in the order they were produced (calendar entry 0 before 4)
        rows = await ReplicaDatabase.session.execute(select(Task).where(Task.brand_id == brand_id).where(Task.name == name).where(Task.status == STATUS_QUEUED).order_by(Task.created_at).limit(1))  # type: ignore[arg-type]
        return rows.scalars().first()

    @staticmethod
    async def for_brand(brand_id: str, statuses: list[str], limit: int = 100) -> list["Task"]:
        """Newest-first tasks in the given statuses — the Work surfaces."""
        rows = await ReplicaDatabase.session.execute(select(Task).where(Task.brand_id == brand_id).where(Task.status.in_(statuses)).order_by(Task.created_at.desc()).limit(limit))  # type: ignore
        return list(rows.scalars().all())

    @staticmethod
    async def in_flight(brand_id: str, name: str) -> list["Task"]:
        """Queued or running tasks of one name — producers check this before
        chaining so the same work is never stacked twice."""
        rows = await ReplicaDatabase.session.execute(select(Task).where(Task.brand_id == brand_id).where(Task.name == name).where(Task.status.in_((STATUS_QUEUED, STATUS_RUNNING))))  # type: ignore
        return list(rows.scalars().all())

    @staticmethod
    async def start_attempt(id: str) -> None:
        """Mark running AND count the attempt — task_lifecycle commits this
        BEFORE the work, so a failed attempt's rollback never un-counts it."""
        task = await Database.session.get(Task, id)
        if task is None:
            raise TaskNotFound()
        task.status = STATUS_RUNNING
        task.attempt_count = (task.attempt_count or 0) + 1
        Database.session.add(task)
        await Database.session.flush()

    @staticmethod
    async def update_status(id: str, status: str) -> None:
        task = await Database.session.get(Task, id)
        if task is None:
            raise TaskNotFound()
        task.status = status
        if status in (STATUS_DONE, STATUS_FAILED, STATUS_DEAD):
            task.finished_at = clock.now()
        Database.session.add(task)
        await Database.session.flush()
