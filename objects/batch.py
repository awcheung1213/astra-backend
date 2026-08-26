import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from constants.constants import UsageKind
from objects.base_object import BaseObject
from utils.database import Database, ReplicaDatabase

KIND_EXECUTE_PROMPTS = "execute_prompts"
KIND_SENTIMENT = "sentiment"


class Batch(SQLModel, BaseObject, table=True):
    """A provider batch job (ported from geoviz-backend). Batch collection
    spans hours and process restarts, so its state lives here, not in a task.

    request_metadata is the decoder ring: per custom_id -> {query_hash,
    query_text, prompt_id, market_id, subscribers} — the cross-brand corpus
    dedup lives in `subscribers`, which is why brand_id is NULLABLE (an
    execute batch belongs to no single brand; a sentiment batch belongs to one).
    Usage is NOT stored here: the ingest job writes cost_events (the single
    cost ledger) when it fetches results. task_id is set only when an agentic
    task submitted the batch — ingest passes it through to the cost_event.
    """

    __tablename__ = "batches"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    provider: str = Field(sa_column=Column(String, nullable=False))  # anthropic | openai | gemini
    batch_id: str = Field(sa_column=Column(String, nullable=False))  # the provider's id
    brand_id: str | None = Field(default=None, sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=True, index=True))
    task_id: str | None = Field(default=None, sa_column=Column(String, ForeignKey("tasks.id"), nullable=True))
    kind: str = Field(default=KIND_EXECUTE_PROMPTS, sa_column=Column(String, server_default=KIND_EXECUTE_PROMPTS, nullable=False))
    status: str = Field(sa_column=Column(String, nullable=False))  # provider-native; poller checks each provider's TERMINAL_STATUSES
    processed: bool = Field(default=False, sa_column=Column(Boolean, server_default="false", nullable=False))
    request_metadata: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="{}"))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, server_default=func.now(), nullable=False))

    @staticmethod
    async def unprocessed(kind: UsageKind | None = None) -> list["Batch"]:
        """In-flight batches the poller still needs to resolve; optionally one kind."""
        statement = select(Batch).where(Batch.processed.is_(False))
        if kind:
            statement = statement.where(Batch.kind == kind)
        rows = await ReplicaDatabase.session.execute(statement)
        return list(rows.scalars().all())

    @staticmethod
    async def update_status(id: str, status: str) -> "Batch":
        batch = await Database.session.get(Batch, id)
        batch.status = status
        Database.session.add(batch)
        await Database.session.flush()
        return batch

    @staticmethod
    async def create(provider: str, batch_id: str, brand_id: str | None, status: str, request_metadata: dict, kind: UsageKind = UsageKind.EXECUTE_PROMPTS) -> "Batch":
        batch = Batch(provider=provider, batch_id=batch_id, brand_id=brand_id, status=status, request_metadata=request_metadata, kind=kind)
        session = Database.session
        session.add(batch)
        await session.flush()
        await session.refresh(batch)
        return batch

    @staticmethod
    async def mark_processed(id: str, status: str) -> "Batch":
        batch = await Database.session.get(Batch, id)
        batch.status = status
        batch.processed = True
        Database.session.add(batch)
        await Database.session.flush()
        return batch
