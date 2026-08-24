import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, Text, func, select
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject
from utils.database import ReplicaDatabase


class Prompt(SQLModel, BaseObject, table=True):
    """A location-agnostic template; the visibility runner expands
    prompts x markets x engines, injecting each market's city."""

    __tablename__ = "prompts"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    brand_id: str = Field(sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True))
    text: str = Field(sa_column=Column(Text, nullable=False))
    category: str
    source: str = "generated"  # generated | user | gsc
    is_active: bool = Field(default=False, sa_column=Column(Boolean, server_default="false", nullable=False))
    # retiring stamps a date, history stays
    retired_at: datetime | None = Field(default=None, sa_column=Column(DateTime, nullable=True))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, server_default=func.now(), nullable=False))

    @staticmethod
    async def by_brand(brand_id: str) -> list["Prompt"]:
        stmt = select(Prompt).where(Prompt.retired_at.is_(None)).where(Prompt.brand_id == brand_id)
        rows = await ReplicaDatabase.session.execute(stmt)
        return list(rows.scalars().all())
