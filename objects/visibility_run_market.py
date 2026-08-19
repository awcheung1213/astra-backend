import uuid

from sqlalchemy import Column, Float, ForeignKey, String, UniqueConstraint
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject


class VisibilityRunMarket(SQLModel, BaseObject, table=True):
    """Per-city scores as rows — 'which market scored lowest' is relational."""

    __tablename__ = "visibility_run_markets"
    __table_args__ = (UniqueConstraint("visibility_run_id", "market_id"),)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    visibility_run_id: str = Field(sa_column=Column(String, ForeignKey("visibility_runs.id", ondelete="CASCADE"), nullable=False, index=True))
    market_id: str = Field(sa_column=Column(String, ForeignKey("markets.id"), nullable=False, index=True))
    overall_score: float = Field(sa_column=Column(Float, nullable=False))
