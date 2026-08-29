import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, Text, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject
from objects.site import STATUS_CONNECTED, STATUS_PENDING
from utils.database import ReplicaDatabase


class Connection(SQLModel, BaseObject, table=True):
    """Read-side integrations — deliberately separate from sites."""

    __tablename__ = "connections"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    brand_id: str = Field(sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True))
    provider: str  # gsc | ga4 | gbp | calls
    status: str = Field(default=STATUS_PENDING, sa_column=Column(String, server_default=STATUS_PENDING, nullable=False))
    credentials_encrypted: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    meta: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="{}"))
    connected_at: datetime | None = Field(default=None, sa_column=Column(DateTime, nullable=True))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, server_default=func.now(), nullable=False))

    @staticmethod
    async def connected_for_brand(brand_id: str) -> list["Connection"]:
        rows = await ReplicaDatabase.session.execute(select(Connection).where(Connection.brand_id == brand_id).where(Connection.status == STATUS_CONNECTED))
        return list(rows.scalars().all())
