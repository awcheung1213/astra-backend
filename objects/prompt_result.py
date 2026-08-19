import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, func
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject

SENTIMENT_POSITIVE = "positive"
SENTIMENT_NEUTRAL = "neutral"
SENTIMENT_NEGATIVE = "negative"


class PromptResult(SQLModel, BaseObject, table=True):
    """A brand's read of a corpus answer — the cheap derived row."""

    __tablename__ = "prompt_results"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    prompt_id: str = Field(sa_column=Column(String, ForeignKey("prompts.id", ondelete="CASCADE"), nullable=False, index=True))
    brand_id: str = Field(sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True))
    visibility_run_id: str | None = Field(default=None, sa_column=Column(String, ForeignKey("visibility_runs.id"), nullable=True, index=True))
    query_response_id: str = Field(sa_column=Column(String, ForeignKey("query_responses.id"), nullable=False, index=True))
    mentioned: bool = Field(default=False, sa_column=Column(Boolean, server_default="false", nullable=False))
    rank: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    sentiment: str | None = Field(default=None, sa_column=Column(String, nullable=True))  # positive | neutral | negative
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, server_default=func.now(), nullable=False))
