import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Index, Integer, Numeric, String, func, select
from sqlalchemy.exc import IntegrityError
from sqlmodel import Field, SQLModel

from exceptions.exceptions import MarketNotFound
from objects.base_object import BaseObject
from utils import clock
from utils.database import Database, ReplicaDatabase

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
    No boundary geometry until a map view returns. Uniqueness is on
    (lower(city), lower(state)) so 'Mesa' and 'mesa' can't both land.
    """

    __tablename__ = "markets"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    city: str
    state: str
    zip: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    # NULL = shared row (system/local); set only on adhoc pseudo-markets
    brand_id: str | None = Field(default=None, sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=True))
    source: str = Field(default=SOURCE_SYSTEM, sa_column=Column(String, nullable=False, server_default=SOURCE_SYSTEM))
    collection_market_id: str | None = Field(default=None, sa_column=Column(String, ForeignKey("markets.id"), nullable=True))
    is_active: bool = Field(default=True, sa_column=Column(Boolean, server_default="true", nullable=False))
    population: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    county_gdp: Decimal | None = Field(default=None, sa_column=Column(Numeric(16, 0), nullable=True))
    census_year: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, default=clock.column_default(), server_default=func.now(), nullable=False))

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
            rows = await ReplicaDatabase.session.execute(select(Market).join(BrandMarket, BrandMarket.market_id == Market.id).where(BrandMarket.brand_id == brand_id, Market.is_active == True, Market.collection_market_id.is_(None)))  # type: ignore # noqa: E712
        else:
            rows = await ReplicaDatabase.session.execute(select(Market).where(Market.brand_id.is_(None), Market.source == SOURCE_SYSTEM, Market.is_active == True, Market.collection_market_id.is_(None)))  # type: ignore # noqa: E712
        return list(rows.scalars().all())

    @staticmethod
    async def all() -> list["Market"]:
        rows = await ReplicaDatabase.session.execute(select(Market))
        return list(rows.scalars().all())

    @staticmethod
    async def get_or_create_city(city: str, state: str) -> "Market":
        """ONE row per physical city (the corpus dedup invariant), three
        layers deep: case-insensitive lookup first (keeping the existing
        row's canonical casing), the case-insensitive unique index as the
        backstop, and a savepoint so a concurrent-create race is caught and
        re-fetched instead of poisoning the request's transaction."""
        city, state = city.strip(), state.strip().upper()
        existing = await Market._by_city_state(city, state)
        if existing:
            return existing
        market = Market(city=city, state=state, source=SOURCE_LOCAL)
        try:
            async with Database.session.begin_nested():
                Database.session.add(market)
        except IntegrityError:
            raced = await Market._by_city_state(city, state)
            if raced is None:
                raise
            return raced
        return market

    @staticmethod
    async def _by_city_state(city: str, state: str) -> "Market | None":
        rows = await Database.session.execute(select(Market).where(func.lower(Market.city) == city.lower(), func.lower(Market.state) == state.lower()))
        return rows.scalars().first()


# the one-row-per-city backstop, case-insensitive (supersedes the old case-sensitive UNIQUE (city, state))
Index("uq_markets_lower_city_state", func.lower(Market.__table__.c.city), func.lower(Market.__table__.c.state), unique=True)
