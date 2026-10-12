import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, func
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject
from utils import clock


class BrandService(SQLModel, BaseObject, table=True):
    __tablename__ = "brand_services"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    brand_id: str = Field(sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True))
    name: str
    # the money-maker; drives prompt generation and content priority
    is_primary: bool = Field(default=False, sa_column=Column(Boolean, server_default="false", nullable=False))
    source: str = "interview"  # interview | site_crawl | user
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, default=clock.column_default(), server_default=func.now(), nullable=False))
