import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, Text, delete, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from exceptions.exceptions import BrandNotFound
from objects.base_object import BaseObject
from objects.brand_market import BrandMarket
from utils import clock
from utils.database import Database, ReplicaDatabase

PLATFORM_WORDPRESS = "wordpress"
PLATFORM_SHOPIFY = "shopify"
PLATFORM_OTHER = "other"
PLATFORM_UNKNOWN = "unknown"

FOOTPRINT_LOCAL = "local"
FOOTPRINT_NATIONAL = "national"


class Brand(SQLModel, BaseObject, table=True):
    __tablename__ = "brands"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    organization_id: str = Field(sa_column=Column(String, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, unique=True))
    name: str
    website_url: str
    platform: str = Field(default=PLATFORM_UNKNOWN, sa_column=Column(String, server_default=PLATFORM_UNKNOWN, nullable=False))
    footprint: str | None = Field(default=None, sa_column=Column(String, nullable=True))  # local | national — set in onboarding step 3
    # review_everything | balanced | hands_off — DISPLAY truth only; the
    # approval_settings rows the preset macro writes are the enforcement truth
    approval_preset: str | None = Field(default=None, sa_column=Column(String, nullable=True))

    # extraction-filled, client-confirmed (verbatim reserve lives in interview_raw)
    description: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    category: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    audience: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    differentiators: list = Field(default_factory=list, sa_column=Column(JSONB, nullable=False, server_default="[]"))
    interview_raw: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="{}"))

    onboarded_at: datetime | None = Field(default=None, sa_column=Column(DateTime, nullable=True))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, default=clock.column_default(), server_default=func.now(), nullable=False))

    @staticmethod
    async def get(id: str) -> "Brand":
        brand = await ReplicaDatabase.session.get(Brand, id)
        if not brand:
            raise BrandNotFound()
        return brand

    @staticmethod
    async def all() -> list["Brand"]:
        rows = await ReplicaDatabase.session.execute(select(Brand))
        return list(rows.scalars().all())

    @staticmethod
    async def launched() -> list["Brand"]:
        """Brands past onboarding launch — the only ones cron work fans out to
        (AC-L19: an abandoned signup generates no paid work)."""
        rows = await ReplicaDatabase.session.execute(select(Brand).where(Brand.onboarded_at.is_not(None)))  # type: ignore
        return list(rows.scalars().all())

    @staticmethod
    async def by_organization_id(organization_id: str) -> list["Brand"]:
        rows = await ReplicaDatabase.session.execute(select(Brand).where(Brand.organization_id == organization_id))  # type: ignore
        return list(rows.scalars().all())

    @staticmethod
    async def update(id: str, **fields) -> "Brand":
        brand = await Database.session.get(Brand, id)
        if not brand:
            raise BrandNotFound()
        for field_name, value in fields.items():
            setattr(brand, field_name, value)
        Database.session.add(brand)
        await Database.session.flush()
        await Database.session.refresh(brand)
        return brand

    @staticmethod
    async def replace_user_markets(brand_id: str, market_ids: list[str]) -> None:
        """The human's coverage becomes exactly `market_ids`: user rows not listed
        are dropped, missing ones subscribed. Machine rows (sampled/suggested)
        are left alone — the wizard only ever edits the human's picks."""
        wanted = list(dict.fromkeys(market_ids))
        rows = await Database.session.execute(select(BrandMarket.market_id).where(BrandMarket.brand_id == brand_id))  # type: ignore
        subscribed = {market_id for (market_id,) in rows}
        await Database.session.execute(delete(BrandMarket).where(BrandMarket.brand_id == brand_id).where(BrandMarket.source == "user").where(BrandMarket.market_id.not_in(wanted)))  # type: ignore
        for market_id in wanted:
            if market_id not in subscribed:
                await BrandMarket.subscribe(brand_id, market_id)
        await Database.session.flush()
