import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Column, DateTime, Integer, Numeric, UniqueConstraint, func, select
from sqlmodel import Field, SQLModel
from utils.database import ReplicaDatabase
from exceptions.exceptions import MarketNotFound

from objects.base_object import BaseObject


SOURCE_SYSTEM = "system"
SOURCE_ADHOC = "adhoc"
# shared city rows created by radius footprints (local brands); like system
# rows they have brand_id NULL, but they never join the NATIONAL daily
# footprint - only the footprints that subscribed to them
SOURCE_LOCAL = "local"
# global pseudo-market for location-independent prompt categories
SOURCE_NATIONAL = "national"


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

    @staticmethod
    async def get(id: str) -> "Market":
        market = await ReplicaDatabase.session.get(Market, id)
        if not market:
            raise MarketNotFound()
        return market

    @staticmethod
    async def by_brand(brand_id: str, brand_footprint: str) -> list["Market"]:
        from objects.brand_market import BrandMarket

        if brand_footprint == "local":
            rows = await ReplicaDatabase.session.execute(select(Market).join(BrandMarket, BrandMarket.market_id == Market.id).where(BrandMarket.brand_id == brand_id, Market.is_active == True))  # type: ignore # noqa: E712
        else:
            rows = await ReplicaDatabase.session.execute(select(Market).where(Market.brand_id.is_(None), Market.source == SOURCE_SYSTEM, Market.is_active == True, Market.collection_market_id.is_(None)))  # type: ignore # noqa: E712
        return list(rows.scalars().all())

    @staticmethod
    async def all() -> list["Market"]:
        rows = await ReplicaDatabase.session.execute(select(Market))
        return list(rows.scalars().all())
