import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject

STATUS_PENDING = "pending"
STATUS_APPLYING = "applying"
STATUS_APPLIED = "applied"
STATUS_FAILED = "failed"
STATUS_REVERTED = "reverted"


class ChangeSet(SQLModel, BaseObject, table=True):
    """The only thing that writes to a client's site. Each operation carries an
    id + completion marker so a retried apply RESUMES, never repeats.
    Append-only: a revert is a NEW change set pointed at by reverted_by_change_set_id."""

    __tablename__ = "change_sets"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    artifact_id: str = Field(sa_column=Column(String, ForeignKey("artifacts.id"), nullable=False, index=True))
    site_id: str = Field(sa_column=Column(String, ForeignKey("sites.id"), nullable=False, index=True))
    brand_id: str = Field(sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True))
    status: str = Field(default=STATUS_PENDING, sa_column=Column(String, server_default=STATUS_PENDING, nullable=False))
    operations: list = Field(default_factory=list, sa_column=Column(JSONB, nullable=False, server_default="[]"))
    applied_at: datetime | None = Field(default=None, sa_column=Column(DateTime, nullable=True))
    reverted_by_change_set_id: str | None = Field(default=None, sa_column=Column(String, ForeignKey("change_sets.id"), nullable=True))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, server_default=func.now(), nullable=False))
