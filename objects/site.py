import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, Text, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from exceptions.exceptions import SiteNotFound
from objects.base_object import BaseObject
from objects.brand import Brand
from utils import clock
from utils.database import Database, ReplicaDatabase

STATUS_PENDING = "pending"
STATUS_CONNECTED = "connected"
STATUS_ERROR = "error"
STATUS_DISCONNECTED = "disconnected"


class Site(SQLModel, BaseObject, table=True):
    """The write target — only sites receive change sets."""

    __tablename__ = "sites"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    brand_id: str = Field(sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True))
    platform: str  # wordpress | shopify
    status: str = Field(default=STATUS_PENDING, sa_column=Column(String, server_default=STATUS_PENDING, nullable=False))
    credentials_encrypted: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    scopes: list = Field(default_factory=list, sa_column=Column(JSONB, nullable=False, server_default="[]"))
    connected_at: datetime | None = Field(default=None, sa_column=Column(DateTime, nullable=True))
    last_verified_at: datetime | None = Field(default=None, sa_column=Column(DateTime, nullable=True))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, default=clock.column_default(), server_default=func.now(), nullable=False))

    @staticmethod
    async def get(id: str) -> "Site":
        site = await ReplicaDatabase.session.get(Site, id)
        if not site:
            raise SiteNotFound()
        return site

    @staticmethod
    async def connected() -> list["Site"]:
        """Connected sites of LAUNCHED brands — the weekly audit's fan-out. A site
        connected mid-wizard gets its first audit from launch, never from cron
        (AC-L19: an abandoned signup generates no paid work)."""
        statement = select(Site).join(Brand, Brand.id == Site.brand_id).where(Site.status == STATUS_CONNECTED).where(Brand.onboarded_at.is_not(None))  # type: ignore
        rows = await ReplicaDatabase.session.execute(statement)
        return list(rows.scalars().all())

    @staticmethod
    async def by_brand(brand_id: str) -> "Site | None":
        rows = await ReplicaDatabase.session.execute(select(Site).where(Site.brand_id == brand_id).where(Site.status == STATUS_CONNECTED))
        return rows.scalars().first()

    @staticmethod
    async def any_for_brand(brand_id: str) -> "Site | None":
        """The brand's site row in ANY status — the connect flow's upsert
        target and the status card's source (by_brand deliberately serves
        only connected rows, so pending/error/disconnected stay inert)."""
        rows = await ReplicaDatabase.session.execute(select(Site).where(Site.brand_id == brand_id).order_by(Site.created_at))  # type: ignore
        return rows.scalars().first()

    @staticmethod
    async def upsert_for_brand(brand_id: str, platform: str, status: str, credentials_encrypted: str | None) -> "Site":
        """One site row per brand, updated in place (the schema allows many;
        the connect flow enforces one)."""
        site = await Site.any_for_brand(brand_id) or Site(brand_id=brand_id, platform=platform)
        site.platform = platform
        site.status = status
        site.credentials_encrypted = credentials_encrypted
        if status == STATUS_CONNECTED:
            site.connected_at = clock.now()
            site.last_verified_at = clock.now()
        Database.session.add(site)
        await Database.session.flush()
        return site
