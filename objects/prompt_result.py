import uuid
from datetime import datetime, timedelta
from exceptions.exceptions import PromptResultNotFound
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, String, func, select, text, update
from sqlmodel import Field, SQLModel
from utils.database import Database, ReplicaDatabase

from objects.base_object import BaseObject

SENTIMENT_POSITIVE = "positive"
SENTIMENT_NEUTRAL = "neutral"
SENTIMENT_NEGATIVE = "negative"


class PromptResult(SQLModel, BaseObject, table=True):
    """A brand's read of a corpus answer — the cheap derived row."""

    __tablename__ = "prompt_results"
    # the cell lookup (freshness windows, newest-analyzed-per-cell) walks
    # (prompt, market, engine) newest-first — one index serves all of them
    __table_args__ = (Index("ix_prompt_results_cell_created", "prompt_id", "market_id", "engine", text("created_at DESC")),)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    prompt_id: str = Field(sa_column=Column(String, ForeignKey("prompts.id", ondelete="CASCADE"), nullable=False, index=True))
    brand_id: str = Field(sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True))
    # denormalized from the corpus row: every scoring/freshness query filters
    # or groups on these (typed-columns rule)
    market_id: str = Field(sa_column=Column(String, ForeignKey("markets.id"), nullable=False))
    engine: str
    visibility_run_id: str | None = Field(default=None, sa_column=Column(String, ForeignKey("visibility_runs.id"), nullable=True, index=True))
    query_response_id: str = Field(sa_column=Column(String, ForeignKey("query_responses.id"), nullable=False, index=True))
    mentioned: bool = Field(default=False, sa_column=Column(Boolean, server_default="false", nullable=False))
    rank: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    # the brand's own domain appears in the answer's citations (citation-rate component)
    brand_cited: bool = Field(default=False, sa_column=Column(Boolean, server_default="false", nullable=False))
    sentiment: str | None = Field(default=None, sa_column=Column(String, nullable=True))  # positive | neutral | negative
    # NULL = sentiment analysis pending (packed batch in flight); scoring only
    # counts analyzed rows — sentiment NULL alone can't distinguish pending/neutral
    analyzed_at: datetime | None = Field(default=None, sa_column=Column(DateTime, nullable=True))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, server_default=func.now(), nullable=False))

    @staticmethod
    async def get(id: str) -> "PromptResult":
        prompt_result = await ReplicaDatabase.session.get(PromptResult, id)
        if not prompt_result:
            raise PromptResultNotFound()
        return prompt_result

    @staticmethod
    async def filter(
        brand_id: str,
        ids: list[str] | None = None,
        market_ids: list[str] | None = None,
        contains_sentiment_analysis: bool | None = None,
    ) -> list["PromptResult"]:
        horizon = datetime.now() - timedelta(days=7)
        statement = (
            select(PromptResult)
            .distinct(PromptResult.prompt_id, PromptResult.market_id, PromptResult.engine)  # type: ignore
            .where(PromptResult.brand_id == brand_id)
            .where(PromptResult.created_at > horizon)  # type: ignore
            .order_by(PromptResult.prompt_id, PromptResult.market_id, PromptResult.engine, PromptResult.created_at.desc())  # type: ignore
        )
        if contains_sentiment_analysis is True:
            statement = statement.where(PromptResult.analyzed_at.isnot(None))  # type: ignore
        elif contains_sentiment_analysis is False:
            statement = statement.where(PromptResult.analyzed_at.is_(None))  # type: ignore
        if ids is not None:
            statement = statement.where(PromptResult.id.in_(ids))
        if market_ids is not None:
            statement = statement.where(PromptResult.market_id.in_(market_ids))

        prompt_results = await ReplicaDatabase.session.execute(statement)
        return list(prompt_results.scalars().all())

    @staticmethod
    async def assign_visibility_run(ids: list[str], visibility_run_id: str) -> None:
        """Stamp the scored results with their run — astra's replacement for
        geoviz's prompt_result <-> visibility_score join table."""
        if not ids:
            return
        await Database.session.execute(update(PromptResult).where(PromptResult.id.in_(ids)).values(visibility_run_id=visibility_run_id))  # type: ignore
        await Database.session.flush()

    @staticmethod
    async def within_freshness_window(brand_id: str, engine: str) -> list["PromptResult"]:
        freshness_window_cutoff = datetime.now() - timedelta(days=7)
        statement = (
            select(PromptResult)
            .distinct(PromptResult.prompt_id, PromptResult.market_id)  # type: ignore
            .where(PromptResult.brand_id == brand_id)
            .where(PromptResult.engine == engine)
            .where(PromptResult.created_at > freshness_window_cutoff)  # type: ignore
            .order_by(PromptResult.prompt_id, PromptResult.market_id, PromptResult.created_at.desc())  # type: ignore
        )
        prompt_results = await ReplicaDatabase.session.execute(statement)
        return list(prompt_results.scalars().all())
