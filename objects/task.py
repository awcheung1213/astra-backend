import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from exceptions.exceptions import TaskNotFound
from objects.base_object import BaseObject
from utils.database import Database

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
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, server_default=func.now(), nullable=False))

    @staticmethod
    async def update_status(id: str, status: str) -> None:
        task = await Database.session.get(Task, id)
        if task is None:
            raise TaskNotFound()
        task.status = status
        if status in (STATUS_DONE, STATUS_FAILED, STATUS_DEAD):
            task.finished_at = datetime.now()
        Database.session.add(task)
        await Database.session.flush()
