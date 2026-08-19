"""The adapter layer — the only door to the outside world.

Every external socket (CMS write, Google read, LLM engines, notifier, R2)
subclasses this. One choke point buys: credentials handling, rate limits,
retries, and per-client cost metering — every call logs a cost_event with an
org id and a price, so retainer margin is a query, not a hope.

Plain typed API clients. No MCP in v1.
"""

from decimal import Decimal

from objects.cost_event import CostEvent
from utils.database import Database


class Adapter:
    name: str = "base"

    async def record_cost(
        self,
        organization_id: str,
        operation: str,
        brand_id: str | None = None,
        task_id: str | None = None,
        tokens: int = 0,
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
                tokens=tokens,
                usd=usd,
            )
        )
        await session.flush()
