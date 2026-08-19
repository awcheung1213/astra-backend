import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, Text, func
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject


class Citation(SQLModel, BaseObject, table=True):
    """Own table so 'top cited domains' is a GROUP BY, not JSONB surgery."""

    __tablename__ = "citations"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    prompt_result_id: str = Field(sa_column=Column(String, ForeignKey("prompt_results.id", ondelete="CASCADE"), nullable=False, index=True))
    url: str = Field(sa_column=Column(Text, nullable=False))
    domain: str = Field(sa_column=Column(String, nullable=False, index=True))
    title: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, server_default=func.now(), nullable=False))
