import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, Text, func, select, update
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject
from utils import clock
from utils.database import Database, ReplicaDatabase


class Prompt(SQLModel, BaseObject, table=True):
    """A location-agnostic template; the visibility runner expands
    prompts x markets x engines, injecting each market's city."""

    __tablename__ = "prompts"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    brand_id: str = Field(sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True))
    text: str = Field(sa_column=Column(Text, nullable=False))
    category: str
    # a 2-4 word plain label for tiles ("Emergency plumber"); NULL = not yet
    # labeled (pre-topic rows until backfill_question_topics runs) — the FE
    # falls back to the question's first words
    topic: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    source: str = "generated"  # generated | user | gsc
    is_active: bool = Field(default=False, sa_column=Column(Boolean, server_default="false", nullable=False))
    # retiring stamps a date, history stays
    retired_at: datetime | None = Field(default=None, sa_column=Column(DateTime, nullable=True))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, default=clock.column_default(), server_default=func.now(), nullable=False))

    @staticmethod
    async def create(brand_id: str, text: str, category: str, source: str = "generated", is_active: bool = False, created_at: datetime | None = None, topic: str | None = None) -> "Prompt":
        """created_at: research stamps its reply order (most-asked first) so
        the rank survives — one transaction's now() would tie every row."""
        prompt = Prompt(brand_id=brand_id, text=text, category=category, source=source, is_active=is_active, topic=topic)
        if created_at is not None:
            prompt.created_at = created_at
        session = Database.session
        session.add(prompt)
        await session.flush()
        await session.refresh(prompt)
        return prompt

    @staticmethod
    async def activate(brand_id: str, ids: list[str]) -> int:
        """Sign-off: flip the chosen prompts active. Scoped to the brand so an
        id from another brand's list is silently a no-op, not a leak."""
        result = await Database.session.execute(update(Prompt).where(Prompt.brand_id == brand_id).where(Prompt.id.in_(ids)).where(Prompt.retired_at.is_(None)).values(is_active=True))  # type: ignore
        await Database.session.flush()
        return result.rowcount

    @staticmethod
    async def by_brand(brand_id: str) -> list["Prompt"]:
        stmt = select(Prompt).where(Prompt.retired_at.is_(None)).where(Prompt.brand_id == brand_id)
        rows = await ReplicaDatabase.session.execute(stmt)
        return list(rows.scalars().all())

    @staticmethod
    async def get_for_brand(brand_id: str, prompt_id: str) -> "Prompt | None":
        """One prompt, only if it is this brand's (retired included: its
        history stays readable). None = not found / another brand's."""
        rows = await ReplicaDatabase.session.execute(select(Prompt).where(Prompt.id == prompt_id).where(Prompt.brand_id == brand_id))  # type: ignore
        return rows.scalars().first()

    @staticmethod
    async def set_topic(prompt_id: str, topic: str) -> bool:
        """Fill a missing topic (the backfill). Only writes while topic IS NULL,
        so a label that landed meanwhile is never overwritten."""
        result = await Database.session.execute(update(Prompt).where(Prompt.id == prompt_id).where(Prompt.topic.is_(None)).values(topic=topic))  # type: ignore
        await Database.session.flush()
        return bool(result.rowcount)
