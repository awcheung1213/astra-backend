import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Column, Date, DateTime, ForeignKey, Numeric, String, UniqueConstraint, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject
from utils.database import Database, ReplicaDatabase


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

    @staticmethod
    async def upsert(brand_id: str, source: str, metric: str, day: date, value: float, dimension: str = "") -> None:
        """Insert-or-replace on the (brand, source, metric, dimension, day)
        identity — re-syncing a window self-corrects late-arriving data."""
        statement = pg_insert(MetricPoint).values(id=str(uuid.uuid4()), brand_id=brand_id, source=source, metric=metric, dimension=dimension, day=day, value=value)
        statement = statement.on_conflict_do_update(index_elements=["brand_id", "source", "metric", "dimension", "day"], set_={"value": statement.excluded.value})
        await Database.session.execute(statement)

    @staticmethod
    async def totals_for_period(brand_id: str, start: date, end: date) -> dict[str, float]:
        """Period totals keyed 'source:metric' — the report assembler's shape."""
        rows = await ReplicaDatabase.session.execute(
            select(MetricPoint.source, MetricPoint.metric, func.sum(MetricPoint.value)).where(MetricPoint.brand_id == brand_id).where(MetricPoint.day >= start).where(MetricPoint.day < end).group_by(MetricPoint.source, MetricPoint.metric)  # type: ignore
        )
        return {f"{source}:{metric}": float(total) for source, metric, total in rows}

    @staticmethod
    async def totals_by_dimension(brand_id: str, source: str, metric: str, start: date, end: date) -> dict[str, float]:
        """One metric's period totals split by dimension (e.g. gsc clicks per
        page) — the decay detector's shape. Dimensionless rows are skipped."""
        rows = await ReplicaDatabase.session.execute(
            select(MetricPoint.dimension, func.sum(MetricPoint.value)).where(MetricPoint.brand_id == brand_id).where(MetricPoint.source == source).where(MetricPoint.metric == metric).where(MetricPoint.day >= start).where(MetricPoint.day < end).group_by(MetricPoint.dimension)  # type: ignore
        )
        return {dimension: float(total) for dimension, total in rows if dimension}
