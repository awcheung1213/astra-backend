import asyncio

import anthropic

import settings
from constants.constants import UsageModel
from schemas.provider_schemas import BatchHandle, CitationContext, LocationContext, QueryRequest, QueryResult
from utils import llm_usage
from utils.logger import logger


class ClaudeService:
    WEB_SEARCH_TOOL_TYPE = "web_search_20250305"
    DEFAULT_MODEL = UsageModel.CLAUDE_HAIKU_4_5_20251001
    # anthropic batch processing_status: in_progress -> canceling -> ended;
    # results are only available once a batch has ended
    TERMINAL_STATUSES = {"ended"}
    DEFAULT_MAX_TOKENS = 1024
    DEFAULT_MAX_WEB_SEARCHES = settings.MAX_WEB_SEARCHES

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        use_web_search: bool = True,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_web_searches: int = DEFAULT_MAX_WEB_SEARCHES,
    ) -> None:
        self.model = model
        self.use_web_search = use_web_search
        self.max_tokens = max_tokens
        self.max_web_searches = max_web_searches
        self.client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

    async def query(
        self,
        prompt: str,
        location: LocationContext | None = None,
        system: str | None = None,
        schema: dict | None = None,
        use_web_search: bool | None = None,
        cache_system: bool = False,
    ) -> QueryResult:
        params = self._build_params(prompt, location, system, schema, use_web_search, cache_system)
        response = await self.client.messages.create(**params)
        return QueryResult(
            custom_id="",
            citations=self._extract_citations(response),
            text=self._extract_text(response),
            structured=self._extract_structured(response) if schema else None,
            raw=response.model_dump(),
            usage=self._extract_usage(response),
        )

    async def query_many(self, requests: list[QueryRequest]) -> list[QueryResult]:
        # bounded: a full-map first fill would otherwise fire hundreds of
        # simultaneous requests; return_exceptions: one failed request must not
        # discard the whole set - completed answers (and their paid usage)
        # still come back
        semaphore = asyncio.Semaphore(settings.ENGINE_MAX_CONCURRENCY)

        async def bounded(req: QueryRequest) -> QueryResult:
            async with semaphore:
                return await self._query_one(req)

        results = await asyncio.gather(*(bounded(req) for req in requests), return_exceptions=True)
        return [result if isinstance(result, QueryResult) else QueryResult(custom_id=req.custom_id, text="", error=f"{type(result).__name__}: {result}") for req, result in zip(requests, results)]

    async def submit_batch(self, requests: list[QueryRequest]) -> BatchHandle:
        batch_requests = [
            {
                "custom_id": req.custom_id,
                "params": self._build_params(req.prompt, req.location, req.system, req.schema, req.use_web_search, req.cache_system),
            }
            for req in requests
        ]
        batch = await self.client.messages.batches.create(requests=batch_requests)
        logger.info(f"claude batch submitted: {batch.id} ({len(batch_requests)} requests)")
        return BatchHandle(provider="claude", batch_id=batch.id, status=batch.processing_status)

    async def get_batch_status(self, batch_id: str) -> str:
        batch = await self.client.messages.batches.retrieve(batch_id)
        return batch.processing_status

    async def get_batch_results(self, batch_id: str) -> list[QueryResult]:
        results: list[QueryResult] = []
        async for entry in await self.client.messages.batches.results(batch_id):
            if entry.result.type == "succeeded":
                results.append(
                    QueryResult(
                        custom_id=entry.custom_id,
                        citations=self._extract_citations(entry.result.message),
                        text=self._extract_text(entry.result.message),
                        structured=self._extract_structured(entry.result.message),
                        raw=entry.result.message.model_dump(),
                        usage=self._extract_usage(entry.result.message),
                    )
                )
            else:
                results.append(
                    QueryResult(
                        custom_id=entry.custom_id,
                        text="",
                        error=f"{entry.result.type}: {getattr(entry.result, 'error', None)}",
                    )
                )
        return results

    async def _query_one(self, req: QueryRequest) -> QueryResult:
        result = await self.query(req.prompt, req.location, req.system, req.schema, req.use_web_search, req.cache_system)
        result.custom_id = req.custom_id
        return result

    def _build_params(
        self,
        prompt: str,
        location: LocationContext | None,
        system: str | None,
        schema: dict | None = None,
        use_web_search: bool | None = None,
        cache_system: bool = False,
    ) -> dict:
        # MAX_WEB_SEARCHES=0 disables the tool outright
        web_search_enabled = (self.use_web_search if use_web_search is None else use_web_search) and self.max_web_searches > 0

        # hydrate the location into BOTH the user message and the system
        # prompt: without it claude tends to decline "near me" questions with
        # "I don't have access to real-time location data" instead of using
        # its web_search tool (observed on live daily runs)
        user_content = prompt
        system_parts = []
        if location:
            zip_suffix = f" {location.zip}" if location.zip else ""
            place = f"{location.city}, {location.state}{zip_suffix}"
            user_content = f"{prompt}\n\nI'm in {place}."
            system_parts.append(
                f"The user is located in {place}. Answer for that location with specific, confident recommendations."
                + (" Use the web_search tool to find current local information." if web_search_enabled else "")
                + " Never decline for lack of location or real-time data - the user's location is provided."
            )
        if system:
            system_parts.append(system)

        params: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": user_content}],
        }
        if system_parts:
            if cache_system:
                # block form so the static instructions carry cache_control and
                # repeated packs bill the shared prefix at 10%
                params["system"] = [{"type": "text", "text": " ".join(system_parts), "cache_control": {"type": "ephemeral"}}]
            else:
                params["system"] = " ".join(system_parts)
        tools: list[dict] = []
        if web_search_enabled:
            tools.append(self._web_search_tool(location))
        if schema:
            tools.append(
                {
                    "name": "structured_output",
                    "description": "Return structured data matching the provided schema.",
                    "input_schema": schema,
                }
            )

        if tools:
            params["tools"] = tools
        if schema:
            params["tool_choice"] = {"type": "tool", "name": "structured_output"}
        return params

    def _web_search_tool(self, location: LocationContext | None) -> dict:
        tool: dict = {
            "type": self.WEB_SEARCH_TOOL_TYPE,
            "name": "web_search",
            "max_uses": self.max_web_searches,
        }
        if location:
            user_loc: dict = {
                "type": "approximate",
                "city": location.city,
                "region": location.state,
                "country": "US",
            }
            tool["user_location"] = user_loc
        return tool

    @staticmethod
    def _extract_usage(message) -> dict | None:
        usage = getattr(message, "usage", None)
        return usage.model_dump() if usage is not None else None

    @staticmethod
    def summarize_usage(results: list[QueryResult]) -> dict:
        """One log-friendly line: token + web-search totals for a set of results."""
        return llm_usage.summarize_usage(results)

    @staticmethod
    def _extract_text(message) -> str:
        return "".join(block.text for block in message.content if getattr(block, "type", None) == "text")

    @staticmethod
    def _extract_structured(message) -> dict | None:
        for block in message.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "structured_output":
                return block.input
        return None

    @staticmethod
    def _extract_citations(message) -> list[CitationContext]:
        citations = []
        for block in message.content:
            block_citations = getattr(block, "citations", None)
            if block_citations is not None:
                for c in block_citations:
                    citations.append(
                        CitationContext(
                            cited_text=c.cited_text,
                            title=c.title,
                            type=c.type,
                            url=c.url,
                        )
                    )
        return citations
