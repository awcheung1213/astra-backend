import uuid
from datetime import date, datetime

from sqlalchemy import Column, Date, DateTime, ForeignKey, String, Text, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject
from utils.database import Database, ReplicaDatabase


class Report(SQLModel, BaseObject, table=True):
    """The monthly client document. Narrative is AM prose over
    assembler-supplied facts. Immutable once published."""

    __tablename__ = "reports"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    brand_id: str = Field(sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True))
    period: date = Field(sa_column=Column(Date, nullable=False))  # first day of the month
    narrative: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    metrics: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="{}"))
    published_at: datetime | None = Field(default=None, sa_column=Column(DateTime, nullable=True))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, server_default=func.now(), nullable=False))

    @staticmethod
    async def create(brand_id: str, period: date, narrative: str, metrics: dict, published_at: datetime | None = None) -> "Report":
        report = Report(brand_id=brand_id, period=period, narrative=narrative, metrics=metrics, published_at=published_at)
        session = Database.session
        session.add(report)
        await session.flush()
        await session.refresh(report)
        return report

    @staticmethod
    async def for_period(brand_id: str, period: date) -> "Report | None":
        rows = await ReplicaDatabase.session.execute(select(Report).where(Report.brand_id == brand_id).where(Report.period == period).limit(1))
        return rows.scalars().first()
