import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, String, Text, func, select
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject
from utils import clock
from utils.database import Database, ReplicaDatabase


class QuestionMove(SQLModel, BaseObject, table=True):
    """One question changing level (core | watching | suggested) — the
    minimal history behind the weekly recap's "question moves" and the
    sorter's monthly swap budget. Typed columns for everything filtered:
    brand + created_at (recap window, month budget), automatic, from_tier."""

    __tablename__ = "question_moves"
    __table_args__ = (Index("ix_question_moves_brand_id_created_at", "brand_id", "created_at"),)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    brand_id: str = Field(sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False))
    prompt_id: str = Field(sa_column=Column(String, ForeignKey("prompts.id", ondelete="CASCADE"), nullable=False, index=True))
    from_tier: str = Field(sa_column=Column(String, nullable=False))
    to_tier: str = Field(sa_column=Column(String, nullable=False))
    reason_code: str = Field(sa_column=Column(String, nullable=False))  # utils/question_tiers.REASONS key
    reason: str = Field(sa_column=Column(Text, nullable=False))  # the plain words shown to the owner
    automatic: bool = Field(default=True, sa_column=Column(Boolean, server_default="true", nullable=False))  # False = the owner did it (or it made room for the owner's pick)
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, default=clock.column_default(), server_default=func.now(), nullable=False))

    @staticmethod
    async def create(brand_id: str, prompt_id: str, from_tier: str, to_tier: str, reason_code: str, reason: str, automatic: bool = True) -> "QuestionMove":
        move = QuestionMove(brand_id=brand_id, prompt_id=prompt_id, from_tier=from_tier, to_tier=to_tier, reason_code=reason_code, reason=reason, automatic=automatic)
        session = Database.session
        session.add(move)
        await session.flush()
        return move

    @staticmethod
    async def since(brand_id: str, since: datetime) -> list["QuestionMove"]:
        """The brand's moves since `since`, oldest first."""
        rows = await ReplicaDatabase.session.execute(select(QuestionMove).where(QuestionMove.brand_id == brand_id).where(QuestionMove.created_at >= since).order_by(QuestionMove.created_at, QuestionMove.id))  # type: ignore
        return list(rows.scalars().all())
