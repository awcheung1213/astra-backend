import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject
from utils import clock


class Snapshot(SQLModel, BaseObject, table=True):
    """Taken before every apply. Blobs live in R2; structure lives here."""

    __tablename__ = "snapshots"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    change_set_id: str = Field(sa_column=Column(String, ForeignKey("change_sets.id", ondelete="CASCADE"), nullable=False, index=True))
    resource_ref: str = Field(sa_column=Column(Text, nullable=False))  # page path / resource identifier
    r2_url: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    before_state: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="{}"))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, default=clock.column_default(), server_default=func.now(), nullable=False))
