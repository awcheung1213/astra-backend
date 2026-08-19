import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, func
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject


class Competitor(SQLModel, BaseObject, table=True):
    """Three states: active = charted; inactive = still matched, history
    accrues; excluded = never matched, never resurrected."""

    __tablename__ = "competitors"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    brand_id: str = Field(sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True))
    name: str
    normalized_name: str
    source: str = "ai_bootstrap"  # ai_bootstrap | user | discovered
    is_active: bool = Field(default=False, sa_column=Column(Boolean, server_default="false", nullable=False))
    is_excluded: bool = Field(default=False, sa_column=Column(Boolean, server_default="false", nullable=False))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, server_default=func.now(), nullable=False))
