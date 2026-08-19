import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Numeric, String, func
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject


class CostEvent(SQLModel, BaseObject, table=True):
    """Written by the adapter layer on every external call — margin is a GROUP BY."""

    __tablename__ = "cost_events"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    organization_id: str = Field(sa_column=Column(String, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True))
    brand_id: str | None = Field(default=None, sa_column=Column(String, ForeignKey("brands.id"), nullable=True, index=True))
    task_id: str | None = Field(default=None, sa_column=Column(String, ForeignKey("tasks.id"), nullable=True))
    adapter: str  # llm_anthropic | llm_openai | llm_google | cms_wp | cms_shopify | google | notifier | storage
    operation: str
    tokens: int = Field(default=0, sa_column=Column(Integer, server_default="0", nullable=False))
    usd: Decimal = Field(default=Decimal("0"), sa_column=Column(Numeric(10, 4), server_default="0", nullable=False))
    occurred_at: datetime = Field(default=None, sa_column=Column(DateTime, server_default=func.now(), nullable=False))
