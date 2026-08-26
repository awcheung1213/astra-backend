import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, select
from sqlmodel import Field, SQLModel, delete, func

from exceptions.exceptions import CompetitorMentionNotFound
from objects.base_object import BaseObject
from utils.database import Database, ReplicaDatabase


class CompetitorMention(SQLModel, BaseObject, table=True):
    __tablename__ = "competitor_mentions"
    # one mention row per (answer, competitor) — the matcher emits at most one
    # entry per competitor per answer, and every reader (report JSON, SoV,
    # prompt-screen percentages) assumes it. Enforced in the DB so a re-analysis
    # bug fails loudly instead of silently inflating competitor visibility.
    # The unique index leads with prompt_result_id, so it also serves the
    # lookups the old single-column index covered.
    __table_args__ = (Index("uq_competitor_mentions_result_name", "prompt_result_id", "normalized_name", unique=True),)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    prompt_result_id: str = Field(sa_column=Column(String, ForeignKey("prompt_results.id", ondelete="CASCADE"), nullable=False))
    competitor_id: str | None = Field(default=None, sa_column=Column(String, ForeignKey("competitors.id", ondelete="CASCADE"), nullable=True))
    competitor_name: str = Field(sa_column=Column(String, nullable=False))
    normalized_name: str = Field(sa_column=Column(String, nullable=False))
    position: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))

    created_at: datetime = Field(default=None, sa_column=Column(DateTime, server_default=func.now(), nullable=False))

    @staticmethod
    async def get(id: str) -> "CompetitorMention":
        mention = await ReplicaDatabase.session.get(CompetitorMention, id)
        if not mention:
            raise CompetitorMentionNotFound()
        return mention

    @staticmethod
    async def all() -> list["CompetitorMention"]:
        rows = await ReplicaDatabase.session.execute(select(CompetitorMention))
        return list(rows.scalars().all())

    @staticmethod
    async def create(
        prompt_result_id: str,
        name: str,
        normalized_name: str,
        competitor_id: str | None = None,
        position: int | None = None,
    ) -> "CompetitorMention":
        mention = CompetitorMention(
            prompt_result_id=prompt_result_id,
            competitor_name=name,
            normalized_name=normalized_name,
            competitor_id=competitor_id,
            position=position,
        )
        session = Database.session
        session.add(mention)
        await session.flush()
        await session.refresh(mention)
        return mention

    @staticmethod
    async def update(
        id: str,
        competitor_id: str | None = None,
        position: int | None = None,
    ) -> "CompetitorMention":
        mention = await CompetitorMention.get(id)
        if competitor_id is not None:
            mention.competitor_id = competitor_id
        if position is not None:
            mention.position = position
        session = Database.session
        session.add(mention)
        await session.flush()
        await session.refresh(mention)
        return mention

    @staticmethod
    async def delete(id: str) -> None:
        await Database.session.execute(delete(CompetitorMention).where(CompetitorMention.id == id))  # type: ignore
