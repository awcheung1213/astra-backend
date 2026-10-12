"""One agent program; each role is config (design doc, Level 2). The persona
half of that config lives in utils/agent_prompts — pick a role with run_role:

    from adapters.agents.claude_agent import ClaudeAgent
    from utils.agent_prompts import ROLE_ANALYST

    diagnosis = await ClaudeAgent().run_role(
        ROLE_ANALYST,
        prompt=f"Brand: {brand.name}. Here are the metrics and the change ledger: ...",
        tools=[GET_METRICS_TOOL, GET_CHANGE_LEDGER_TOOL],
        handlers={"get_metrics": get_metrics, "get_change_ledger": get_change_ledger},
        operation="diagnosis", brand_id=brand.id, task_id=task.id,
    )

The system prompt stays byte-identical per role (cache_control makes repeat
turns bill at ~10%); everything brand- and task-specific rides `prompt`."""

from typing import Any, Awaitable, Callable

import anthropic

import settings
from constants.constants import UsageModel
from objects.cost_event import CostEvent
from utils.agent_prompts import system_prompt_for
from utils.llm_usage import CACHE_READ_MULTIPLIER, CACHE_WRITE_MULTIPLIER, estimate_cost
from utils.logger import logger

ToolHandler = Callable[[dict], Awaitable[str]]
UsageHook = Callable[[Any], Awaitable[None]]

AGENT_MAX_ITERATIONS = 25


def metered_input(usage) -> tuple[int, int]:
    """(total prompt tokens, base-rate-equivalent tokens for pricing). The API's
    input_tokens is only the UNCACHED remainder: cache writes/reads — where a
    turn's server-side web-search iterations re-read their results — are
    reported separately and would otherwise go unmetered (BUG-052). Pure."""
    uncached = getattr(usage, "input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    return uncached + cache_write + cache_read, round(uncached + cache_write * CACHE_WRITE_MULTIPLIER + cache_read * CACHE_READ_MULTIPLIER)


class AgentRefusal(Exception):
    """The safety classifiers declined a turn (HTTP 200, stop_reason=refusal)."""


class AgentIterationLimit(Exception):
    """The loop hit max_iterations while the model was still calling tools."""


class ClaudeAgent:
    DEFAULT_MODEL = UsageModel.CLAUDE_OPUS_5
    DEFAULT_MAX_WEB_SEARCHES = settings.MAX_WEB_SEARCHES
    AGENT_MODEL = "claude-opus-5"
    AGENT_MAX_TOKENS = 16000

    def __init__(
        self,
        model: str = AGENT_MODEL,
        use_web_search: bool = True,
        max_tokens: int = AGENT_MAX_TOKENS,
        max_web_searches: int = DEFAULT_MAX_WEB_SEARCHES,
    ) -> None:
        self.model = model
        self.use_web_search = use_web_search
        self.max_tokens = max_tokens
        self.max_web_searches = max_web_searches
        self.client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

    async def agent_turn(
        self,
        messages: list[dict],
        system: str | None = None,
        tools: list[dict] | None = None,
        model: str = AGENT_MODEL,
        max_tokens: int = AGENT_MAX_TOKENS,
    ) -> anthropic.types.Message:
        """One turn of an agent loop (adapters/agents/): full conversation
        history in, raw Message out (content blocks + stop_reason + usage).

        Deliberately NOT routed through _build_params — location hydration,
        web-search wiring, and the forced structured_output tool_choice are
        collection-specific and wrong for agent turns. Tools are the caller's
        (arbitrary definitions, tool_choice auto); the system block carries
        cache_control because the loop resends the same prefix every turn.
        """
        params: dict = {"model": model, "max_tokens": max_tokens, "messages": messages}
        if system:
            params["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        if tools:
            params["tools"] = tools
        return await self.client.messages.create(**params)

    async def run_role(
        self,
        role: str,
        prompt: str,
        tools: list[dict],
        handlers: dict[str, ToolHandler],
        **run_agent_kwargs,
    ) -> str:
        """run_agent with the role's system prompt (utils/agent_prompts) —
        the normal entry point for worker runners. KeyError on unknown role."""
        return await self.run_agent(system=system_prompt_for(role), prompt=prompt, tools=tools, handlers=handlers, **run_agent_kwargs)

    async def run_agent(
        self,
        system: str,
        prompt: str,
        tools: list[dict],
        handlers: dict[str, ToolHandler],
        max_iterations: int = AGENT_MAX_ITERATIONS,
        operation: str | None = None,
        organization_id: str | None = None,
        brand_id: str | None = None,
        task_id: str | None = None,
        on_usage: UsageHook | None = None,
    ) -> str:
        """Run one agent task to completion; returns the final assistant text.

        Passing `operation` (+ org/brand/task ids) writes one cost_events row
        per turn, committed on its own session so a failed attempt's spend
        survives the task rollback; omit it to skip metering (e.g. local
        tinkering without a DB session). The worker task function injects the rest: handlers that
        touch the outside world call other adapters, and on_usage receives
        each turn's usage so the caller enforces the task's LIFETIME token
        budget (raise from on_usage to stop the run). Writes to client sites
        are never tools: the
        agent proposes (a handler inserts a change_sets row); only the
        change-set engine applies, behind the approval gate (rules 1 and 7).

        Each iteration is one Messages API call — the role:"user" turns
        appended mid-loop are tool results, not a human. max_iterations is the
        runaway backstop next to the token budget.
        """
        messages: list[dict] = [{"role": "user", "content": prompt}]
        for iteration in range(1, max_iterations + 1):
            response = await self.agent_turn(messages, system=system, tools=tools)
            if operation is not None:
                usage = response.usage
                server_tool_use = getattr(usage, "server_tool_use", None)
                web_search_count = (getattr(server_tool_use, "web_search_requests", 0) or 0) if server_tool_use else 0
                input_tokens, priced_input_tokens = metered_input(usage)
                await CostEvent.create(
                    adapter="claude",
                    operation=operation,
                    organization_id=organization_id,
                    brand_id=brand_id,
                    task_id=task_id,
                    model=response.model,
                    input_tokens=input_tokens,
                    output_tokens=usage.output_tokens,
                    web_search_count=web_search_count,
                    usd=estimate_cost("claude", response.model, priced_input_tokens, usage.output_tokens, web_search_count),
                    own_transaction=True,  # spend is real even if the task attempt rolls back
                )
            if on_usage is not None:
                await on_usage(response.usage)

            if response.stop_reason == "refusal":
                raise AgentRefusal(f"refused on iteration {iteration}")
            if response.stop_reason == "pause_turn":
                # a server-side tool paused mid-turn; resend to let it resume
                messages.append({"role": "assistant", "content": response.content})
                continue
            if response.stop_reason != "tool_use":
                if response.stop_reason == "max_tokens":
                    logger.warning(f"agent turn truncated at max_tokens on iteration {iteration}")
                return "".join(block.text for block in response.content if block.type == "text")

            messages.append({"role": "assistant", "content": response.content})
            results: list[dict] = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                handler = handlers.get(block.name)
                if handler is None:
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": f"unknown tool: {block.name}", "is_error": True})
                    continue
                try:
                    output = await handler(block.input)
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
                except Exception as exc:
                    logger.warning(f"agent tool {block.name} failed: {type(exc).__name__}: {exc}")
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": f"{type(exc).__name__}: {exc}", "is_error": True})
            # all results for a turn ride ONE user message — splitting them
            # trains the model to stop making parallel tool calls
            messages.append({"role": "user", "content": results})
        raise AgentIterationLimit(f"still calling tools after {max_iterations} iterations")
