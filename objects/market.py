import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Column, DateTime, Integer, Numeric, UniqueConstraint, func
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject


class Market(SQLModel, BaseObject, table=True):
    """Shared: ONE row per physical city — the corpus dedup invariant.

    Census columns justify market selection from data (population x county GDP
    picks the 150-market national sample); census_year makes staleness visible.
    No boundary geometry until a map view returns.
    """

    __tablename__ = "markets"
    __table_args__ = (UniqueConstraint("city", "state"),)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    city: str
    state: str
    population: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    county_gdp: Decimal | None = Field(default=None, sa_column=Column(Numeric(16, 0), nullable=True))
    census_year: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, server_default=func.now(), nullable=False))
