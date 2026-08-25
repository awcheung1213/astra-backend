"""The adapter layer — the only door to the outside world.

Every external socket (CMS write, Google read, LLM engines, notifier, R2)
subclasses this. One choke point buys: credentials handling, rate limits,
retries, and per-client cost metering — every call logs a cost_event with a
price (org id when attributable; NULL = system COGS), so retainer margin is a
query, not a hope. Batch API calls log at ingest time, when usage is known.

Plain typed API clients. No MCP in v1.
"""

from decimal import Decimal

from objects.cost_event import CostEvent
from utils import llm_usage
from utils.database import Database


class Adapter:
    name: str = "base"

    async def record_llm_usage(
        self,
        usage: dict | None,
        provider: str,
        model: str,
        operation: str | None = None,
        organization_id: str | None = None,
        brand_id: str | None = None,
        task_id: str | None = None,
        batch: bool = False,
    ) -> None:
        """Join the attribution flowing IN from the call site (operation +
        org/brand/task; organization_id None = system COGS) with the usage
        flowing OUT of the provider response into one cost_events row. Every
        engine normalizes usage to Anthropic-style keys, so this works for all
        three. No-op without an operation or usage (a caller that doesn't want
        metering simply passes no operation)."""
        if operation is None or usage is None:
            return
        input_tokens = usage.get("input_tokens") or 0
        output_tokens = usage.get("output_tokens") or 0
        web_searches = ((usage.get("server_tool_use") or {}).get("web_search_requests")) or 0
        usd = llm_usage.estimate_cost(provider, model, input_tokens, output_tokens, web_searches, batch=batch)
        await self.record_cost(
            operation=operation,
            organization_id=organization_id,
            brand_id=brand_id,
            task_id=task_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            web_search_count=web_searches,
            usd=Decimal(str(usd)),
        )

    async def record_cost(
        self,
        operation: str,
        organization_id: str | None = None,
        brand_id: str | None = None,
        task_id: str | None = None,
        model: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        web_search_count: int = 0,
        usd: Decimal = Decimal("0"),
    ) -> None:
        # inside an API request the middleware owns the session (flush, never
        # commit); standalone worker code opens its own connection and commits
        session = Database.session
        session.add(
            CostEvent(
                organization_id=organization_id,
                brand_id=brand_id,
                task_id=task_id,
                adapter=self.name,
                operation=operation,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                web_search_count=web_search_count,
                usd=usd,
            )
        )
        await session.flush()
