import uuid
from datetime import date, datetime

from sqlalchemy import Column, Date, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject


class Report(SQLModel, BaseObject, table=True):
    """The monthly client document. Narrative is AM prose over
    assembler-supplied facts. Immutable once published."""

    __tablename__ = "reports"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    brand_id: str = Field(sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True))
    period: date = Field(sa_column=Column(Date, nullable=False))  # first day of the month
    narrative: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    metrics: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="{}"))
    published_at: datetime | None = Field(default=None, sa_column=Column(DateTime, nullable=True))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, server_default=func.now(), nullable=False))
