import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Numeric, String, func
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject
from utils.database import Database


class CostEvent(SQLModel, BaseObject, table=True):
    """The dollar ledger — margin is a GROUP BY. Written manually at the spend
    sites (batch ingest in Collector.poll_batches, per turn in
    ClaudeAgent.run_agent, future CMS/notifier adapters as they land).

    organization_id is NULLABLE: corpus collection is system COGS, attributable
    to no org (the shared-corpus economics make any per-org split arbitrary).
    Token columns are typed (not a jsonb blob) so usd stays recomputable —
    input/output/web-search prices differ per model.
    """

    __tablename__ = "cost_events"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    organization_id: str | None = Field(default=None, sa_column=Column(String, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True))
    brand_id: str | None = Field(default=None, sa_column=Column(String, ForeignKey("brands.id"), nullable=True, index=True))
    task_id: str | None = Field(default=None, sa_column=Column(String, ForeignKey("tasks.id"), nullable=True))
    adapter: str  # llm_anthropic | llm_openai | llm_google | cms_wp | cms_shopify | google | notifier | storage
    operation: str
    model: str | None = Field(default=None, sa_column=Column(String, nullable=True))  # provider model id, LLM adapters only
    input_tokens: int = Field(default=0, sa_column=Column(Integer, server_default="0", nullable=False))
    output_tokens: int = Field(default=0, sa_column=Column(Integer, server_default="0", nullable=False))
    web_search_count: int = Field(default=0, sa_column=Column(Integer, server_default="0", nullable=False))
    usd: Decimal = Field(default=Decimal("0"), sa_column=Column(Numeric(10, 4), server_default="0", nullable=False))
    occurred_at: datetime = Field(default=None, sa_column=Column(DateTime, server_default=func.now(), nullable=False))

    @staticmethod
    async def create(
        adapter: str,
        operation: str,
        organization_id: str | None = None,
        brand_id: str | None = None,
        task_id: str | None = None,
        model: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        web_search_count: int = 0,
        usd: float = 0.0,
    ) -> "CostEvent":
        cost_event = CostEvent(
            adapter=adapter,
            operation=operation,
            organization_id=organization_id,
            brand_id=brand_id,
            task_id=task_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            web_search_count=web_search_count,
            # via str: Decimal(float) would capture the float's binary noise
            usd=Decimal(str(usd)),
        )
        session = Database.session
        session.add(cost_event)
        await session.flush()
        await session.refresh(cost_event)
        return cost_event
