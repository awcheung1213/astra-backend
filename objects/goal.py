import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Column, Date, DateTime, ForeignKey, Numeric, String, func, select
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject
from utils.database import ReplicaDatabase

METRIC_LEADS = "leads"
METRIC_REVENUE = "revenue"


class Goal(SQLModel, BaseObject, table=True):
    __tablename__ = "goals"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    brand_id: str = Field(sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True))
    metric: str  # leads | revenue
    target: Decimal = Field(sa_column=Column(Numeric(14, 2), nullable=False))
    deadline: date | None = Field(default=None, sa_column=Column(Date, nullable=True))
    retired_at: datetime | None = Field(default=None, sa_column=Column(DateTime, nullable=True))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, server_default=func.now(), nullable=False))

    @staticmethod
    async def active(brand_id: str) -> list["Goal"]:
        rows = await ReplicaDatabase.session.execute(select(Goal).where(Goal.brand_id == brand_id).where(Goal.retired_at.is_(None)))  # type: ignore
        return list(rows.scalars().all())
