from typing import Any, Awaitable, Callable

import anthropic

import settings
from constants.constants import UsageModel
from objects.cost_event import CostEvent
from utils.llm_usage import estimate_cost
from utils.logger import logger

ToolHandler = Callable[[dict], Awaitable[str]]
UsageHook = Callable[[Any], Awaitable[None]]

AGENT_MAX_ITERATIONS = 25


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
        per turn; omit it to skip metering (e.g. local tinkering without a DB
        session). The worker task function injects the rest: handlers that
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
                await CostEvent.create(
                    adapter="claude",
                    operation=operation,
                    organization_id=organization_id,
                    brand_id=brand_id,
                    task_id=task_id,
                    model=response.model,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    usd=estimate_cost("claude", response.model, usage.input_tokens, usage.output_tokens),
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
