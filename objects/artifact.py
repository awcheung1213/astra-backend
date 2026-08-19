import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject

STATUS_PENDING_REVIEW = "pending_review"
STATUS_APPROVED = "approved"
STATUS_AUTO_APPROVED = "auto_approved"
STATUS_REJECTED = "rejected"


class Artifact(SQLModel, BaseObject, table=True):
    """Every agent/pipeline output. Only site-touching types convert to change sets."""

    __tablename__ = "artifacts"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    task_id: str = Field(sa_column=Column(String, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True))
    brand_id: str = Field(sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True))
    type: str  # change_proposal | draft | pitch | reply | flag_request | diagnosis | plan | brief
    content: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="{}"))
    evidence: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="{}"))
    status: str = Field(default=STATUS_PENDING_REVIEW, sa_column=Column(String, server_default=STATUS_PENDING_REVIEW, nullable=False))
    reviewed_by: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    reviewed_at: datetime | None = Field(default=None, sa_column=Column(DateTime, nullable=True))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, server_default=func.now(), nullable=False))
