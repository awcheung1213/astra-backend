import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, Text, UniqueConstraint, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject
from objects.site import STATUS_CONNECTED, STATUS_DISCONNECTED, STATUS_PENDING
from utils import clock
from utils.database import Database, ReplicaDatabase


class Connection(SQLModel, BaseObject, table=True):
    """Read-side integrations — deliberately separate from sites."""

    __tablename__ = "connections"
    # one row per provider per brand: the consumers (research, metrics, gbp)
    # all do first-match-wins lookups, so a duplicate would be a silent coin flip
    __table_args__ = (UniqueConstraint("brand_id", "provider"),)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    brand_id: str = Field(sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True))
    provider: str  # gsc | ga4 | gbp | calls | gmail | outlook
    status: str = Field(default=STATUS_PENDING, sa_column=Column(String, server_default=STATUS_PENDING, nullable=False))
    credentials_encrypted: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    meta: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="{}"))
    connected_at: datetime | None = Field(default=None, sa_column=Column(DateTime, nullable=True))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, default=clock.column_default(), server_default=func.now(), nullable=False))

    @staticmethod
    async def connected_for_brand(brand_id: str) -> list["Connection"]:
        rows = await ReplicaDatabase.session.execute(select(Connection).where(Connection.brand_id == brand_id).where(Connection.status == STATUS_CONNECTED))
        return list(rows.scalars().all())

    @staticmethod
    async def for_brand(brand_id: str) -> list["Connection"]:
        """Every provider row in ANY status — the Connect status card."""
        rows = await ReplicaDatabase.session.execute(select(Connection).where(Connection.brand_id == brand_id).order_by(Connection.created_at))  # type: ignore
        return list(rows.scalars().all())

    @staticmethod
    async def get_for_brand_provider(brand_id: str, provider: str) -> "Connection | None":
        rows = await ReplicaDatabase.session.execute(select(Connection).where(Connection.brand_id == brand_id).where(Connection.provider == provider))  # type: ignore
        return rows.scalars().first()

    @staticmethod
    async def upsert(brand_id: str, provider: str, status: str, credentials_encrypted: str | None, meta: dict | None = None) -> "Connection":
        """One row per (brand, provider), updated in place — backed by the
        unique constraint. meta (non-secret display facts, e.g. a mailbox's
        address) replaces the stored meta when given."""
        connection = await Connection.get_for_brand_provider(brand_id, provider) or Connection(brand_id=brand_id, provider=provider)
        connection.status = status
        connection.credentials_encrypted = credentials_encrypted
        if meta is not None:
            connection.meta = meta
        if status == STATUS_CONNECTED:
            connection.connected_at = clock.now()
        Database.session.add(connection)
        await Database.session.flush()
        return connection

    @staticmethod
    async def mark_disconnected(brand_id: str, provider: str) -> None:
        """The provider refused the stored grant (revoked/expired): forget the
        secret, keep the row (and its meta) so the UI can say "reconnect"."""
        connection = await Connection.get_for_brand_provider(brand_id, provider)
        if connection is not None:
            connection.status = STATUS_DISCONNECTED
            connection.credentials_encrypted = None
            Database.session.add(connection)
            await Database.session.flush()

    @staticmethod
    async def update_meta(brand_id: str, provider: str, **facts) -> None:
        """Merge non-secret facts into the row's meta (e.g. synced_at)."""
        connection = await Connection.get_for_brand_provider(brand_id, provider)
        if connection is not None:
            connection.meta = {**(connection.meta or {}), **facts}
            Database.session.add(connection)
            await Database.session.flush()

    @staticmethod
    async def update_credentials(brand_id: str, provider: str, credentials_encrypted: str) -> None:
        """Store a rotated secret (Microsoft rotates refresh tokens) in place."""
        connection = await Connection.get_for_brand_provider(brand_id, provider)
        if connection is not None:
            connection.credentials_encrypted = credentials_encrypted
            Database.session.add(connection)
            await Database.session.flush()

    @staticmethod
    async def delete_for_brand_provider(brand_id: str, provider: str) -> None:
        connection = await Connection.get_for_brand_provider(brand_id, provider)
        if connection is not None:
            await Database.session.delete(connection)
            await Database.session.flush()
