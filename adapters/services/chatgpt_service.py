import asyncio
import io
import json
import uuid

from openai import AsyncOpenAI

import settings
from constants.constants import UsageModel
from schemas.provider_schemas import BatchHandle, CitationContext, LocationContext, QueryRequest, QueryResult
from utils.logger import logger


class ChatGPTService:
    WEB_SEARCH_TOOL_TYPE = "web_search"
    DEFAULT_MODEL = UsageModel.GPT_5_4_NANO_2026_03_17
    BATCH_ENDPOINT = "/v1/responses"
    BATCH_COMPLETION_WINDOW = "24h"
    DEFAULT_MAX_WEB_SEARCHES = settings.MAX_WEB_SEARCHES
    # openai batch statuses: validating -> in_progress -> finalizing -> completed
    # (or failed / expired / cancelled). Results only exist past a terminal state.
    TERMINAL_STATUSES = {"completed", "failed", "expired", "cancelled"}

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        use_web_search: bool = True,
        max_web_searches: int = DEFAULT_MAX_WEB_SEARCHES,
    ) -> None:
        self.model = model
        self.use_web_search = use_web_search
        self.max_web_searches = max_web_searches
        self.client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    async def query(
        self,
        prompt: str,
        location: LocationContext | None = None,
        system: str | None = None,
        schema: dict | None = None,
        use_web_search: bool | None = None,
        cache_system: bool = False,  # openai caches prompt prefixes automatically; accepted for interface parity
    ) -> QueryResult:
        params = self._build_params(prompt, location, system, schema, use_web_search)
        response = await self.client.responses.create(**params)
        body = response.model_dump()
        return QueryResult(
            custom_id="",
            text=self._extract_text(body),
            citations=self._extract_citations(body),
            structured=self._extract_structured(body) if schema else None,
            raw=body,
            usage=self._extract_usage(body),
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
        jsonl_lines = [
            json.dumps(
                {
                    "custom_id": req.custom_id,
                    "method": "POST",
                    "url": self.BATCH_ENDPOINT,
                    "body": self._build_params(req.prompt, req.location, req.system, req.schema, req.use_web_search),
                }
            )
            for req in requests
        ]
        payload = ("\n".join(jsonl_lines)).encode("utf-8")
        file_obj = await self.client.files.create(
            file=(f"batch-{uuid.uuid4()}.jsonl", io.BytesIO(payload)),
            purpose="batch",
        )
        batch = await self.client.batches.create(
            input_file_id=file_obj.id,
            endpoint=self.BATCH_ENDPOINT,
            completion_window=self.BATCH_COMPLETION_WINDOW,
        )
        logger.info(f"chatgpt batch submitted: {batch.id} ({len(requests)} requests)")
        return BatchHandle(
            provider="chatgpt",
            batch_id=batch.id,
            status=batch.status,
            extra={"input_file_id": file_obj.id},
        )

    async def get_batch_status(self, batch_id: str) -> str:
        batch = await self.client.batches.retrieve(batch_id)
        return batch.status

    async def get_batch_results(self, batch_id: str) -> list[QueryResult]:
        batch = await self.client.batches.retrieve(batch_id)
        results: list[QueryResult] = []
        for file_id in (batch.output_file_id, batch.error_file_id):
            if not file_id:
                continue
            content = await self.client.files.content(file_id)
            body = await content.aread() if hasattr(content, "aread") else content.read()
            for line in body.decode("utf-8").splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                custom_id = entry.get("custom_id", "")
                response = entry.get("response") or {}
                if entry.get("error") or (response.get("status_code") or 200) != 200:
                    results.append(QueryResult(custom_id=custom_id, text="", error=str(entry.get("error") or response.get("body"))))
                    continue
                response_body = response.get("body") or {}
                results.append(
                    QueryResult(
                        custom_id=custom_id,
                        text=self._extract_text(response_body),
                        citations=self._extract_citations(response_body),
                        structured=self._extract_structured(response_body),
                        raw=response_body,
                        usage=self._extract_usage(response_body),
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
    ) -> dict:
        params: dict = {
            "model": self.model,
            "input": prompt,
        }
        instruction_parts = []
        if location:
            zip_suffix = f" {location.zip}" if location.zip else ""
            instruction_parts.append(f"The user is located in {location.city}, {location.state}{zip_suffix}.")
        if system:
            instruction_parts.append(system)
        if instruction_parts:
            params["instructions"] = " ".join(instruction_parts)
        if schema:
            params["text"] = {"format": {"type": "json_schema", "name": "output", "schema": schema, "strict": True}}
        # MAX_WEB_SEARCHES=0 disables the tool outright (same lever as claude)
        web_search_enabled = (self.use_web_search if use_web_search is None else use_web_search) and self.max_web_searches > 0
        if web_search_enabled:
            params["tools"] = [self._web_search_tool(location)]
            # openai has no per-tool max_uses; max_tool_calls caps built-in tool
            # calls for the whole request, which is equivalent with one tool
            params["max_tool_calls"] = self.max_web_searches
            # DEC-023: gpt-5.4-nano on "auto" answered local questions without
            # searching (0 citations); "required" forces the (only) tool, so
            # every answer is grounded like claude/gemini (~+$0.01/answer)
            params["tool_choice"] = "required"
        return params

    def _web_search_tool(self, location: LocationContext | None) -> dict:
        tool: dict = {"type": self.WEB_SEARCH_TOOL_TYPE}
        if location:
            user_loc: dict = {
                "type": "approximate",
                "country": "US",
                "city": location.city,
                "region": location.state,
            }
            tool["user_location"] = user_loc
        return tool

    @staticmethod
    def _extract_text(body: dict) -> str:
        parts: list[str] = []
        for item in body.get("output") or []:
            if item.get("type") != "message":
                continue
            for block in item.get("content") or []:
                text = block.get("text")
                if text:
                    parts.append(text)
        return "".join(parts)

    @staticmethod
    def _extract_citations(body: dict) -> list[CitationContext]:
        citations: list[CitationContext] = []
        for item in body.get("output") or []:
            if item.get("type") != "message":
                continue
            for block in item.get("content") or []:
                text = block.get("text") or ""
                for annotation in block.get("annotations") or []:
                    if annotation.get("type") != "url_citation":
                        continue
                    start, end = annotation.get("start_index") or 0, annotation.get("end_index") or 0
                    citations.append(
                        CitationContext(
                            cited_text=text[start:end],
                            title=annotation.get("title") or "",
                            type="url_citation",
                            url=annotation.get("url") or "",
                        )
                    )
        return citations

    @staticmethod
    def _extract_structured(body: dict) -> dict | None:
        text = ChatGPTService._extract_text(body)
        try:
            result = json.loads(text) if text else None
            return result if isinstance(result, dict) else None
        except (json.JSONDecodeError, ValueError):
            return None

    @staticmethod
    def _extract_usage(body: dict) -> dict | None:
        usage = body.get("usage")
        if usage is None:
            return None
        searches = sum(1 for item in body.get("output") or [] if item.get("type") == "web_search_call")
        # cached_tokens is a subset of input_tokens: split it out, Anthropic-style (BUG-053)
        cached = (usage.get("input_tokens_details") or {}).get("cached_tokens") or 0
        return {
            "input_tokens": (usage.get("input_tokens") or 0) - cached,
            "cache_read_input_tokens": cached,
            "output_tokens": usage.get("output_tokens") or 0,
            "server_tool_use": {"web_search_requests": searches},
        }
