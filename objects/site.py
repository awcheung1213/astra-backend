import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, Text, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject
from utils.database import ReplicaDatabase

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
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, server_default=func.now(), nullable=False))

    @staticmethod
    async def connected() -> list["Site"]:
        """Only connected sites receive audits/change sets."""
        rows = await ReplicaDatabase.session.execute(select(Site).where(Site.status == STATUS_CONNECTED))
        return list(rows.scalars().all())

    @staticmethod
    async def by_brand(brand_id: str) -> "Site | None":
        rows = await ReplicaDatabase.session.execute(select(Site).where(Site.brand_id == brand_id).where(Site.status == STATUS_CONNECTED))
        return rows.scalars().first()
