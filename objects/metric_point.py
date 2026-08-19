import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Column, Date, DateTime, ForeignKey, Numeric, String, UniqueConstraint, func
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject


class MetricPoint(SQLModel, BaseObject, table=True):
    """One shape for every external number; upsert on re-sync.
    dimension defaults to '' (not NULL) so the unique constraint holds —
    NULLs never collide in Postgres."""

    __tablename__ = "metric_points"
    __table_args__ = (UniqueConstraint("brand_id", "source", "metric", "dimension", "day"),)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    brand_id: str = Field(sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True))
    source: str  # ga4 | gsc | calls | shopify
    metric: str
    dimension: str = Field(default="", sa_column=Column(String, server_default="", nullable=False))
    day: date = Field(sa_column=Column(Date, nullable=False))
    value: Decimal = Field(sa_column=Column(Numeric(16, 2), nullable=False))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, server_default=func.now(), nullable=False))
