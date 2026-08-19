import uuid
from datetime import date, datetime

from sqlalchemy import Column, Date, DateTime, Float, ForeignKey, String, func
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject

FRAMEWORK_VERSION = "1.0.0"  # weights live in code, version stamped per run

STATUS_COLLECTING = "collecting"
STATUS_SCORING = "scoring"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


class VisibilityRun(SQLModel, BaseObject, table=True):
    """One row per weekly sweep = the trend point. Components are TYPED, not
    jsonb — a fixed, framework-versioned set that gets filtered and trended."""

    __tablename__ = "visibility_runs"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    brand_id: str = Field(sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True))
    week_of: date = Field(sa_column=Column(Date, nullable=False))
    status: str = Field(default=STATUS_COLLECTING, sa_column=Column(String, server_default=STATUS_COLLECTING, nullable=False))
    overall_score: float | None = Field(default=None, sa_column=Column(Float, nullable=True))
    detection_rate: float | None = Field(default=None, sa_column=Column(Float, nullable=True))
    share_of_voice: float | None = Field(default=None, sa_column=Column(Float, nullable=True))
    positive_rate: float | None = Field(default=None, sa_column=Column(Float, nullable=True))
    top3_rate: float | None = Field(default=None, sa_column=Column(Float, nullable=True))
    citation_rate: float | None = Field(default=None, sa_column=Column(Float, nullable=True))
    framework_version: str = Field(default=FRAMEWORK_VERSION)
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, server_default=func.now(), nullable=False))
