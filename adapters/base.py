"""The adapter layer — the only door to the outside world.

Every external socket (CMS write, Google read, LLM engines, notifier, R2)
lives under adapters/. One choke point buys: credentials handling, rate
limits, retries. Cost events are written MANUALLY at the spend sites via
CostEvent.create() — batch ingest (Collector.poll_batches) and the agent
loop (ClaudeAgent.run_agent) — never at submit time, when cost is unknown.

Plain typed API clients. No MCP in v1.
"""


class Adapter:
    name: str = "base"
