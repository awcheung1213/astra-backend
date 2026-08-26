import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject

MODE_AUTO = "auto"
MODE_APPROVE = "approve"


class ApprovalSetting(SQLModel, BaseObject, table=True):
    """Presets are macros that write these rows; the always-approve floor
    (outward/irreversible task names) is enforced in code, never a row."""

    __tablename__ = "approval_settings"
    __table_args__ = (UniqueConstraint("brand_id", "task_name"),)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    brand_id: str = Field(sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True))
    task_name: str
    mode: str = Field(default=MODE_APPROVE, sa_column=Column(String, server_default=MODE_APPROVE, nullable=False))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, server_default=func.now(), nullable=False))
