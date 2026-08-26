import uuid
from datetime import date, datetime

from sqlalchemy import Column, Date, DateTime, Float, ForeignKey, String, UniqueConstraint, func, select
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject
from utils.database import Database, ReplicaDatabase

FRAMEWORK_VERSION = "1.0.0"  # weights live in code, version stamped per run

STATUS_COLLECTING = "collecting"
STATUS_SCORING = "scoring"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


class VisibilityRun(SQLModel, BaseObject, table=True):
    """One row per weekly sweep = the trend point. Components are TYPED, not
    jsonb — a fixed, framework-versioned set that gets filtered and trended."""

    __tablename__ = "visibility_runs"
    # one trend point per brand-week; retried sweeps resume the existing row
    __table_args__ = (UniqueConstraint("brand_id", "week_of"),)

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

    @staticmethod
    async def get_or_create_for_week(brand_id: str, week_of: date) -> "VisibilityRun":
        """Idempotent per brand-week: a retried sweep resumes the existing run
        instead of minting a duplicate trend point."""
        rows = await ReplicaDatabase.session.execute(select(VisibilityRun).where(VisibilityRun.brand_id == brand_id).where(VisibilityRun.week_of == week_of))
        existing = rows.scalars().first()
        if existing:
            return existing
        run = VisibilityRun(brand_id=brand_id, week_of=week_of, status=STATUS_SCORING)
        session = Database.session
        session.add(run)
        await session.flush()
        await session.refresh(run)
        return run

    @staticmethod
    async def finish(
        id: str,
        overall_score: float,
        detection_rate: float,
        share_of_voice: float,
        positive_rate: float,
        top3_rate: float,
        citation_rate: float,
    ) -> "VisibilityRun":
        run = await Database.session.get(VisibilityRun, id)
        run.overall_score = overall_score
        run.detection_rate = detection_rate
        run.share_of_voice = share_of_voice
        run.positive_rate = positive_rate
        run.top3_rate = top3_rate
        run.citation_rate = citation_rate
        run.status = STATUS_DONE
        session = Database.session
        session.add(run)
        await session.flush()
        await session.refresh(run)
        return run

    @staticmethod
    async def update_status(id: str, status: str) -> None:
        run = await Database.session.get(VisibilityRun, id)
        if run is None:
            return
        run.status = status
        Database.session.add(run)
        await Database.session.flush()

    @staticmethod
    async def latest(brand_id: str, limit: int = 2) -> list["VisibilityRun"]:
        """Newest-first trend points — latest(brand, 2) gives the WoW delta."""
        rows = await ReplicaDatabase.session.execute(select(VisibilityRun).where(VisibilityRun.brand_id == brand_id).where(VisibilityRun.status == STATUS_DONE).order_by(VisibilityRun.week_of.desc()).limit(limit))  # type: ignore
        return list(rows.scalars().all())
