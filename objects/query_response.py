import uuid
from datetime import datetime, timedelta

from sqlalchemy import Column, DateTime, ForeignKey, String, Text, func, select, tuple_
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel
from utils.database import ReplicaDatabase

from objects.base_object import BaseObject

ENGINE_CHATGPT = "chatgpt"
ENGINE_GEMINI = "gemini"
ENGINE_CLAUDE = "claude"


class QueryResponse(SQLModel, BaseObject, table=True):
    """BRAND-LESS shared corpus. INSERT-ONLY. Keyed by (query_hash, market, engine) —
    one paid answer, read free by every brand tracking that text there."""

    __tablename__ = "query_responses"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    query_hash: str = Field(sa_column=Column(String, nullable=False, index=True))
    query_text: str = Field(sa_column=Column(Text, nullable=False))
    market_id: str = Field(sa_column=Column(String, ForeignKey("markets.id"), nullable=False, index=True))
    engine: str
    text: str = Field(sa_column=Column(Text, nullable=False))
    # raw citation capture at collection time (the answer's sources, brand-less);
    # analysis materializes per-brand citation ROWS from this without re-collecting
    citations: list = Field(default_factory=list, sa_column=Column(JSONB, nullable=False, server_default="[]"))
    raw: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="{}"))
    collected_at: datetime = Field(default=None, sa_column=Column(DateTime, server_default=func.now(), nullable=False))

    @staticmethod
    async def within_freshness_window(stale_cells: set[tuple[str, str]], engine: str) -> list["QueryResponse"]:
        freshness_window_cutoff = datetime.now() - timedelta(days=7)
        statement = (
            select(QueryResponse)
            .distinct(QueryResponse.query_hash, QueryResponse.market_id)
            .where(tuple_(QueryResponse.query_hash, QueryResponse.market_id).in_(stale_cells))
            .where(QueryResponse.engine == engine)
            .where(QueryResponse.collected_at > freshness_window_cutoff)
            .order_by(QueryResponse.query_hash, QueryResponse.market_id, QueryResponse.collected_at.desc())
        )
        query_responses = await ReplicaDatabase.session.execute(statement)
        return list(query_responses.scalars().all())