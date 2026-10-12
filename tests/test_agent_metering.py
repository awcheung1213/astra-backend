"""BUG-052 — run_agent meters prompt-cache tokens: input_tokens from the API
is only the uncached remainder, and a web-search turn's result tokens arrive
as cache writes/reads. Pure (fake client, CostEvent monkeypatched)."""

from types import SimpleNamespace

import adapters.agents.claude_agent as claude_agent
from adapters.agents.claude_agent import ClaudeAgent, metered_input


def _usage(input_tokens=500, output_tokens=8048, cache_write=0, cache_read=0, searches=4):
    return SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens, cache_creation_input_tokens=cache_write, cache_read_input_tokens=cache_read, server_tool_use=SimpleNamespace(web_search_requests=searches))


def test_metered_input_adds_cache_tokens_at_their_rates():
    assert metered_input(_usage(input_tokens=105, cache_write=7345, cache_read=7123)) == (14573, round(105 + 7345 * 1.25 + 7123 * 0.1))
    assert metered_input(SimpleNamespace(input_tokens=500)) == (500, 500)  # older usage objects without cache fields
    assert metered_input(SimpleNamespace(input_tokens=500, cache_creation_input_tokens=None, cache_read_input_tokens=None)) == (500, 500)


async def test_run_agent_cost_event_carries_cache_tokens_and_search_price(monkeypatch):
    rows: list[dict] = []

    async def create(**kwargs):
        rows.append(kwargs)

    response = SimpleNamespace(model="claude-opus-5", usage=_usage(cache_write=20000, cache_read=40000), stop_reason="end_turn", content=[SimpleNamespace(type="text", text="done")])

    async def turn(self, messages, system=None, tools=None, **kwargs):
        return response

    monkeypatch.setattr(claude_agent.CostEvent, "create", create)
    monkeypatch.setattr(ClaudeAgent, "agent_turn", turn)
    monkeypatch.setattr(claude_agent.anthropic, "AsyncAnthropic", lambda **kwargs: None)

    assert await ClaudeAgent().run_agent(system="s", prompt="p", tools=[], handlers={}, operation="outreach_scout") == "done"
    row = rows[0]
    assert row["input_tokens"] == 60500 and row["web_search_count"] == 4
    # (500 + 20000*1.25 + 40000*0.1) * $5/MTok + 8048 * $25/MTok + 4 * $0.01
    assert row["usd"] == round((500 + 25000 + 4000) * 5 / 1_000_000 + 8048 * 25 / 1_000_000 + 0.04, 6)
