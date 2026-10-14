import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Index, Integer, Numeric, String, func, select
from sqlalchemy.exc import IntegrityError
from sqlmodel import Field, SQLModel

import settings
from exceptions.exceptions import MarketNotFound
from objects.base_object import BaseObject
from utils import clock
from utils.city_panel import candidate, promotion_plan, select_panel
from utils.database import Database, ReplicaDatabase

SOURCE_SYSTEM = "system"
SOURCE_ADHOC = "adhoc"
# shared city rows created by radius footprints (local brands); like system
# rows they have brand_id NULL, but they never join the NATIONAL daily
# footprint - only the footprints that subscribed to them
SOURCE_LOCAL = "local"
# global pseudo-market for location-independent prompt categories: ONE row
# (city/state "-"), collected with no location context; national brands'
# panel includes it (Lean collection, utils/city_panel.py)
SOURCE_NATIONAL = "national"
NATIONWIDE_CITY = NATIONWIDE_STATE = "-"


class Market(SQLModel, BaseObject, table=True):
    """Shared: ONE row per physical city — the corpus dedup invariant.

    Census columns justify market selection from data (population x county GDP
    picks the ~150 seeded system markets; population picks the active national
    panel among them — panel_rank, utils/city_panel.py); census_year makes
    staleness visible.
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
    # national city panel rank 1..NATIONAL_PANEL_SIZE (system rows only); NULL =
    # not on the panel (inactive, kept for history + borderline re-asks)
    panel_rank: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, default=clock.column_default(), server_default=func.now(), nullable=False))

    @staticmethod
    async def get(id: str) -> "Market":
        market = await ReplicaDatabase.session.get(Market, id)
        if not market:
            raise MarketNotFound()
        return market

    @staticmethod
    async def by_brand(brand_id: str, brand_footprint: str) -> list["Market"]:
        """The brand's collection markets, most important first (the budget
        governor trims from the end). Local: its active home cities in
        subscription order. National: the nationwide pseudo-market + the
        active system panel by rank (capped at NATIONAL_PANEL_SIZE; population
        orders any unranked active row)."""
        from objects.brand_market import BrandMarket

        if brand_footprint == "local":
            rows = await ReplicaDatabase.session.execute(select(Market).join(BrandMarket, BrandMarket.market_id == Market.id).where(BrandMarket.brand_id == brand_id, Market.is_active == True, Market.collection_market_id.is_(None)).order_by(BrandMarket.created_at, BrandMarket.id))  # type: ignore # noqa: E712
            return list(rows.scalars().all())
        nationwide = await ReplicaDatabase.session.execute(select(Market).where(Market.brand_id.is_(None), Market.source == SOURCE_NATIONAL, Market.is_active == True).limit(1))  # type: ignore # noqa: E712
        panel = await ReplicaDatabase.session.execute(
            select(Market)
            .where(Market.brand_id.is_(None), Market.source == SOURCE_SYSTEM, Market.is_active == True, Market.collection_market_id.is_(None))  # type: ignore # noqa: E712
            .order_by(Market.panel_rank.asc().nulls_last(), Market.population.desc().nulls_last(), Market.id)  # type: ignore
            .limit(settings.NATIONAL_PANEL_SIZE)
        )
        return [*nationwide.scalars().all(), *panel.scalars().all()]

    @staticmethod
    async def reask_pool() -> list["Market"]:
        """Non-panel shared system collector rows (active or not) — the
        borderline re-ask's candidates; utils.city_panel.reask_cities ranks them."""
        rows = await ReplicaDatabase.session.execute(select(Market).where(Market.brand_id.is_(None), Market.source == SOURCE_SYSTEM, Market.collection_market_id.is_(None), Market.panel_rank.is_(None)))  # type: ignore
        return list(rows.scalars().all())

    @staticmethod
    async def nationwide() -> "Market":
        """The ONE nationwide pseudo-market row, created on first use (same
        savepoint race handling as get_or_create_city)."""
        existing = await Market._by_city_state(NATIONWIDE_CITY, NATIONWIDE_STATE)
        if existing:
            return existing
        market = Market(city=NATIONWIDE_CITY, state=NATIONWIDE_STATE, source=SOURCE_NATIONAL)
        try:
            async with Database.session.begin_nested():
                Database.session.add(market)
        except IntegrityError:
            raced = await Market._by_city_state(NATIONWIDE_CITY, NATIONWIDE_STATE)
            if raced is None:
                raise
            return raced
        return market

    @staticmethod
    async def apply_panel(size: int | None = None) -> dict[str, int]:
        """Rank the panel over the ACTIVE shared system collector rows and
        deactivate the rest of them (rows kept). Ensures the nationwide row.
        Used by seed.py after seeding; the migration did the same for existing
        DBs. Flushes, never commits. Returns {market id: rank}."""
        size = size or settings.NATIONAL_PANEL_SIZE
        rows = await Database.session.execute(select(Market).where(Market.brand_id.is_(None), Market.source == SOURCE_SYSTEM, Market.collection_market_id.is_(None), Market.is_active == True))  # type: ignore # noqa: E712
        markets = list(rows.scalars().all())
        ranks = select_panel([candidate(market) for market in markets], size)
        for market in markets:
            market.panel_rank = ranks.get(market.id)
            market.is_active = market.id in ranks
            Database.session.add(market)
        await Market.nationwide()
        await Database.session.flush()
        return ranks

    @staticmethod
    async def promote_panel_replacements(size: int | None = None, exclude_ids: tuple[str, ...] = ()) -> dict[str, int]:
        """Operator helper (no cron): after a panel city was deactivated, hand
        its rank to the next candidate (utils.city_panel.promotion_plan) and
        clear the deactivated row's rank. exclude_ids: system cities that must
        never be promoted. Flushes, never commits. Returns {market id: rank}."""
        size = size or settings.NATIONAL_PANEL_SIZE
        rows = await Database.session.execute(select(Market).where(Market.brand_id.is_(None), Market.source == SOURCE_SYSTEM, Market.collection_market_id.is_(None)))  # type: ignore
        markets = list(rows.scalars().all())
        vacated = [market for market in markets if market.panel_rank is not None and not market.is_active]
        vacated_ids = {market.id for market in vacated}
        members = [(candidate(market), market.panel_rank) for market in markets if market.panel_rank is not None and market.is_active]
        pool = [candidate(market) for market in markets if market.panel_rank is None and not market.is_active and market.id not in exclude_ids and market.id not in vacated_ids]
        promoted = promotion_plan(members, pool, size)
        for market in markets:
            if market.id in vacated_ids:
                market.panel_rank = None
                Database.session.add(market)
            elif market.id in promoted:
                market.panel_rank = promoted[market.id]
                market.is_active = True
                Database.session.add(market)
        await Database.session.flush()
        return promoted

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
