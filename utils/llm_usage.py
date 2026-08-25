"""Provider usage totals and estimated dollar cost for LLM calls.

Every engine normalizes its usage payload to Anthropic-style keys
(`input_tokens`, `output_tokens`, `server_tool_use.web_search_requests`), so
one summarizer works on mixed-engine result sets.

Prices are our own estimates (USD), maintained by hand — update the tables
(and the last-verified date) when provider pricing changes or an engine's
model is swapped. Rates last verified 2026-08-12 against LiteLLM's
model_prices_and_context_window.json. Token rates are per million tokens;
batch API traffic is billed at half the token rate by all three providers
(web searches are not discounted). Cache-creation/cache-read tokens are
ignored, matching what the summarizer counts. An unknown model falls back to
its provider's default-model pricing so a known provider's row is never
silently $0.
"""

BATCH_TOKEN_DISCOUNT = 0.5

# model prefix -> ($ per MTok input, $ per MTok output, $ per web search)
MODEL_PRICING: dict[str, dict[str, tuple[float, float, float]]] = {
    "claude": {
        "claude-haiku-4-5": (1.00, 5.00, 0.01),
        "claude-sonnet": (3.00, 15.00, 0.01),
        "claude-opus": (5.00, 25.00, 0.01),
    },
    # Gemini 3 grounding is billed per search QUERY ($14/1k, after 5k free
    # queries/month) on top of tokens — the free tier is ignored here
    "gemini": {
        "gemini": (0.25, 1.50, 0.014),
    },
    "chatgpt": {
        "gpt": (0.20, 1.25, 0.01),
    },
}

# provider -> pricing of its DEFAULT_MODEL, for models with no prefix match
DEFAULT_PRICING: dict[str, tuple[float, float, float]] = {
    "claude": MODEL_PRICING["claude"]["claude-haiku-4-5"],
    "gemini": MODEL_PRICING["gemini"]["gemini"],
    "chatgpt": MODEL_PRICING["chatgpt"]["gpt"],
}


def summarize_usage(results: list) -> dict:
    """Token + web-search totals for a set of QueryResults (any engine mix)."""
    input_tokens = output_tokens = searches = 0
    for result in results:
        usage = result.usage or {}
        input_tokens += usage.get("input_tokens") or 0
        output_tokens += usage.get("output_tokens") or 0
        searches += ((usage.get("server_tool_use") or {}).get("web_search_requests")) or 0
    return dict(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        web_searches=searches,
    )


def estimate_cost(provider: str, model: str, input_tokens: int, output_tokens: int, web_searches: int = 0, batch: bool = False) -> float:
    rates = None
    for prefix in sorted(MODEL_PRICING.get(provider, {}), key=len, reverse=True):
        if model.startswith(prefix):
            rates = MODEL_PRICING[provider][prefix]
            break
    rates = rates or DEFAULT_PRICING.get(provider)
    if rates is None:
        return 0.0
    input_rate, output_rate, search_rate = rates
    token_cost = (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
    if batch:
        token_cost *= BATCH_TOKEN_DISCOUNT
    return round(token_cost + web_searches * search_rate, 6)
