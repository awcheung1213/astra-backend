import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject

PLATFORM_WORDPRESS = "wordpress"
PLATFORM_SHOPIFY = "shopify"
PLATFORM_OTHER = "other"
PLATFORM_UNKNOWN = "unknown"

FOOTPRINT_LOCAL = "local"
FOOTPRINT_NATIONAL = "national"


class Brand(SQLModel, BaseObject, table=True):
    __tablename__ = "brands"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    organization_id: str = Field(sa_column=Column(String, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, unique=True))
    name: str
    website_url: str
    platform: str = Field(default=PLATFORM_UNKNOWN, sa_column=Column(String, server_default=PLATFORM_UNKNOWN, nullable=False))
    footprint: str | None = Field(default=None, sa_column=Column(String, nullable=True))  # local | national — set in onboarding step 3

    # extraction-filled, client-confirmed (verbatim reserve lives in interview_raw)
    description: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    category: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    audience: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    differentiators: list = Field(default_factory=list, sa_column=Column(JSONB, nullable=False, server_default="[]"))
    interview_raw: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="{}"))

    onboarded_at: datetime | None = Field(default=None, sa_column=Column(DateTime, nullable=True))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, server_default=func.now(), nullable=False))
