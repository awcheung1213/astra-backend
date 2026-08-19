import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, func
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject


class User(SQLModel, BaseObject, table=True):
    __tablename__ = "users"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    organization_id: str = Field(sa_column=Column(String, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True))
    supabase_user_id: str = Field(sa_column=Column(String, nullable=False, unique=True))
    email: str
    role: str = "admin"
    phone: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    sms_urgent_enabled: bool = Field(default=False, sa_column=Column(Boolean, server_default="false", nullable=False))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, server_default=func.now(), nullable=False))
