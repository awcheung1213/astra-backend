import uuid

from sqlalchemy import Column, Float, ForeignKey, String, UniqueConstraint
from sqlmodel import Field, SQLModel, delete
from utils.database import Database

from objects.base_object import BaseObject


class VisibilityRunMarket(SQLModel, BaseObject, table=True):
    """Per-city scores as rows — 'which market scored lowest' is relational."""

    __tablename__ = "visibility_run_markets"
    __table_args__ = (UniqueConstraint("visibility_run_id", "market_id"),)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    visibility_run_id: str = Field(sa_column=Column(String, ForeignKey("visibility_runs.id", ondelete="CASCADE"), nullable=False, index=True))
    market_id: str = Field(sa_column=Column(String, ForeignKey("markets.id"), nullable=False, index=True))
    overall_score: float = Field(sa_column=Column(Float, nullable=False))

    @staticmethod
    async def create(visibility_run_id: str, market_id: str, overall_score: float) -> "VisibilityRunMarket":
        row = VisibilityRunMarket(visibility_run_id=visibility_run_id, market_id=market_id, overall_score=overall_score)
        session = Database.session
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return row

    @staticmethod
    async def delete_for_run(visibility_run_id: str) -> None:
        """A resumed sweep rebuilds its market rows from scratch (unique
        constraint would otherwise reject the re-insert)."""
        await Database.session.execute(delete(VisibilityRunMarket).where(VisibilityRunMarket.visibility_run_id == visibility_run_id))  # type: ignore
