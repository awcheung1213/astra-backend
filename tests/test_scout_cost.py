"""The outreach scout runs on Sonnet, weekly journalist scans (cost_events showed
the scout as the biggest line: Opus, ~$0.45/run, a daily journalist scan)."""

from types import SimpleNamespace

import worker.tasks.outreach as outreach
from adapters.agents.claude_agent import ClaudeAgent


async def test_run_agent_uses_the_model_it_was_built_with(monkeypatch):
    seen = {}

    async def turn(self, messages, system=None, tools=None, **kwargs):
        seen.update(kwargs)
        usage = SimpleNamespace(input_tokens=10, output_tokens=5, server_tool_use=None)
        return SimpleNamespace(usage=usage, model=kwargs.get("model"), stop_reason="end_turn", content=[SimpleNamespace(type="text", text="[]")])

    monkeypatch.setattr(ClaudeAgent, "agent_turn", turn)
    await ClaudeAgent(model=outreach.SCOUT_MODEL).run_agent(system="s", prompt="p", tools=[], handlers={})
    assert seen["model"] == outreach.SCOUT_MODEL and outreach.SCOUT_MODEL.startswith("claude-sonnet")


def test_journalist_scan_is_weekly():
    import inspect

    import worker.tasks.schedules as schedules

    source = inspect.getsource(schedules)
    assert '@app.periodic(cron="0 12 * * 2")  # Tuesdays 12:00 UTC' in source and '@app.periodic(cron="0 13 * * *")' not in source
