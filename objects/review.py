import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, func
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject


class Review(SQLModel, BaseObject, table=True):
    __tablename__ = "reviews"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    brand_id: str = Field(sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True))
    platform: str  # google | trustpilot | g2 | yelp
    external_id: str
    rating: int = Field(sa_column=Column(Integer, nullable=False))
    author: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    text: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    received_at: datetime = Field(sa_column=Column(DateTime, nullable=False))
    # the drafted reply is an artifact; this row just points at it
    reply_artifact_id: str | None = Field(default=None, sa_column=Column(String, ForeignKey("artifacts.id"), nullable=True))
    replied_at: datetime | None = Field(default=None, sa_column=Column(DateTime, nullable=True))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, server_default=func.now(), nullable=False))
