import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint, func, select
from sqlalchemy.exc import IntegrityError
from sqlmodel import Field, SQLModel
from utils.database import Database, ReplicaDatabase
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
    zip: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    # NULL = shared row (system/local); set only on adhoc pseudo-markets
    brand_id: str | None = Field(default=None, sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=True))
    source: str = Field(default=SOURCE_SYSTEM, sa_column=Column(String, nullable=False, server_default=SOURCE_SYSTEM))
    collection_market_id: str | None = Field(default=None, sa_column=Column(String, ForeignKey("markets.id"), nullable=True))
    is_active: bool = Field(default=True, sa_column=Column(Boolean, server_default="true", nullable=False))
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
            rows = await ReplicaDatabase.session.execute(select(Market).join(BrandMarket, BrandMarket.market_id == Market.id).where(BrandMarket.brand_id == brand_id, Market.is_active == True, Market.collection_market_id.is_(None)))  # type: ignore # noqa: E712
        else:
            rows = await ReplicaDatabase.session.execute(select(Market).where(Market.brand_id.is_(None), Market.source == SOURCE_SYSTEM, Market.is_active == True, Market.collection_market_id.is_(None)))  # type: ignore # noqa: E712
        return list(rows.scalars().all())

    @staticmethod
    async def all() -> list["Market"]:
        rows = await ReplicaDatabase.session.execute(select(Market))
        return list(rows.scalars().all())

    @staticmethod
    async def search(query: str, limit: int = 10) -> list["Market"]:
        """City-prefix typeahead over the shared active rows — the wizard's
        coverage step. Biggest cities first so 'pho' leads with Phoenix."""
        if not query.strip():
            return []
        rows = await ReplicaDatabase.session.execute(
            select(Market).where(Market.brand_id.is_(None), Market.is_active == True, Market.collection_market_id.is_(None), Market.city.ilike(f"{query.strip()}%")).order_by(Market.population.desc().nulls_last(), Market.city).limit(limit)  # type: ignore # noqa: E712
        )
        return list(rows.scalars().all())

    @staticmethod
    async def get_or_create_city(city: str, state: str) -> "Market":
        """ONE row per physical city (the corpus dedup invariant), three
        layers deep: case-insensitive lookup first (keeping the existing
        row's canonical casing), the (city, state) unique constraint as the
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
