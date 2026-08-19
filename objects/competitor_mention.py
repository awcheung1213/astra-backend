import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, func
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject


class CompetitorMention(SQLModel, BaseObject, table=True):
    """What makes share-of-voice computable."""

    __tablename__ = "competitor_mentions"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    prompt_result_id: str = Field(sa_column=Column(String, ForeignKey("prompt_results.id", ondelete="CASCADE"), nullable=False, index=True))
    competitor_id: str = Field(sa_column=Column(String, ForeignKey("competitors.id", ondelete="CASCADE"), nullable=False, index=True))
    rank: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, server_default=func.now(), nullable=False))
