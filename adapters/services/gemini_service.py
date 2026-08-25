import asyncio
import json

import httpx
from google import genai
from google.genai import types

import settings
from constants.constants import UsageModel
from objects.citation import domain_from_url
from schemas.provider_schemas import BatchHandle, CitationContext, LocationContext, QueryRequest, QueryResult
from utils.logger import logger


GROUNDING_REDIRECT_HOST = "vertexaisearch.cloud.google.com"
# TUNING (if batch ingest ever gets slow here): 20 is generously sufficient
# until a single gemini batch reaches roughly tens-of-thousands of answers —
# resolution costs ~150ms per unique redirect, so e.g. 300k uniques at 20 is
# ~37min of poll time. The fix at that point is this one constant: ~100 is
# safe because it's one Google edge host, HEAD-only, connection-pooled by
# httpx (300k urls -> ~7min, inside a cron that already tolerates long
# ingests). The REAL ceiling isn't the semaphore, it's whether Google
# throttles the redirect endpoint at that request rate — unknowable until
# you're there, non-fatal by design (throttled resolves just keep the
# redirect url), and it will announce itself in the "resolved N/M" log line.
# At 100k+ answers per batch the architectural upgrade is a lazy/deferred
# resolution pass instead of inline ingest — machinery ahead of need today.
REDIRECT_RESOLVE_CONCURRENCY = 20
REDIRECT_RESOLVE_TIMEOUT = 5.0


class GeminiService:
    DEFAULT_MODEL = UsageModel.GEMINI_3_1_FLASH_LITE
    DEFAULT_MAX_WEB_SEARCHES = settings.MAX_WEB_SEARCHES
    TERMINAL_STATUSES = {"JOB_STATE_SUCCEEDED", "JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        use_web_search: bool = True,
        max_web_searches: int = DEFAULT_MAX_WEB_SEARCHES,
    ) -> None:
        self.model = model
        self.use_web_search = use_web_search
        # gemini's google_search grounding has no per-request query cap (the
        # engine decides how many searches a grounded prompt fires, billed per
        # query) - so this knob only gates the tool: 0 disables it outright,
        # any positive value enables grounding. Kept for interface parity and
        # so the cost lever reads the same across engines.
        self.max_web_searches = max_web_searches
        self.client = genai.Client(api_key=settings.GEMINI_API_KEY)

    async def query(
        self,
        prompt: str,
        location: LocationContext | None = None,
        system: str | None = None,
        schema: dict | None = None,
        use_web_search: bool | None = None,
        cache_system: bool = False,  # gemini caches prompt prefixes implicitly; accepted for interface parity
    ) -> QueryResult:
        config = self._build_config(location, system, schema, use_web_search)
        response = await self.client.aio.models.generate_content(
            model=self.model,
            contents=prompt,
            config=config,
        )
        result = QueryResult(
            custom_id="",
            text=getattr(response, "text", "") or "",
            citations=self._extract_citations(response),
            structured=self._extract_structured(response) if schema else None,
            raw=response.model_dump(mode="json") if hasattr(response, "model_dump") else None,
            usage=self._extract_usage(response),
        )
        await self._resolve_citation_redirects([result])
        return result

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
        # metadata carries the caller's custom_id; results fall back to request
        # order (the API returns inlined responses in submission order)
        inline_requests = [
            types.InlinedRequest(
                contents=[types.Content(role="user", parts=[types.Part(text=req.prompt)])],
                config=self._build_config(req.location, req.system, req.schema, req.use_web_search),
                metadata={"custom_id": req.custom_id},
            )
            for req in requests
        ]
        batch = await self.client.aio.batches.create(
            model=self.model,
            src=inline_requests,
        )
        logger.info(f"gemini batch submitted: {batch.name} ({len(requests)} requests)")
        return BatchHandle(
            provider="gemini",
            batch_id=batch.name or "",
            status=self._state_name(batch),
            extra={"custom_ids": [req.custom_id for req in requests]},
        )

    async def get_batch_status(self, batch_id: str) -> str:
        batch = await self.client.aio.batches.get(name=batch_id)
        return self._state_name(batch)

    async def get_batch_results(self, batch_id: str) -> list[QueryResult]:
        batch = await self.client.aio.batches.get(name=batch_id)
        if self._state_name(batch) not in self.TERMINAL_STATUSES:
            return []

        results: list[QueryResult] = []
        inlined = getattr(getattr(batch, "dest", None), "inlined_responses", None) or []
        for idx, entry in enumerate(inlined):
            metadata = getattr(entry, "metadata", None) or {}
            custom_id = metadata.get("custom_id") or str(idx)
            if getattr(entry, "error", None):
                results.append(QueryResult(custom_id=custom_id, text="", error=str(entry.error)))
                continue
            response = entry.response
            results.append(
                QueryResult(
                    custom_id=custom_id,
                    text=getattr(response, "text", "") or "",
                    citations=self._extract_citations(response),
                    structured=self._extract_structured(response),
                    raw=response.model_dump(mode="json") if hasattr(response, "model_dump") else None,
                    usage=self._extract_usage(response),
                )
            )
        await self._resolve_citation_redirects(results)
        return results

    @staticmethod
    async def _resolve_citation_redirects(results: list[QueryResult]) -> None:
        """Swap grounding redirect urls for the real source urls."""
        redirect_urls = {citation.url for result in results for citation in (result.citations or []) if citation.url and GROUNDING_REDIRECT_HOST in citation.url}
        if not redirect_urls:
            return

        resolved: dict[str, str] = {}
        semaphore = asyncio.Semaphore(REDIRECT_RESOLVE_CONCURRENCY)
        async with httpx.AsyncClient(follow_redirects=False, timeout=REDIRECT_RESOLVE_TIMEOUT) as client:

            async def resolve(url: str) -> None:
                async with semaphore:
                    try:
                        response = await client.head(url)
                        location = response.headers.get("location")
                        if location and location.startswith("http"):
                            resolved[url] = location
                    except Exception:  # noqa - unresolved citations keep the redirect url
                        pass

            await asyncio.gather(*(resolve(url) for url in redirect_urls))

        for result in results:
            for citation in result.citations or []:
                source_url = resolved.get(citation.url)
                if source_url:
                    citation.url = source_url
                    citation.domain = domain_from_url(source_url) or citation.domain

    async def _query_one(self, req: QueryRequest) -> QueryResult:
        result = await self.query(req.prompt, req.location, req.system, req.schema, req.use_web_search, req.cache_system)
        result.custom_id = req.custom_id
        return result

    def _build_config(
        self,
        location: LocationContext | None,
        system: str | None,
        schema: dict | None = None,
        use_web_search: bool | None = None,
    ) -> types.GenerateContentConfig:
        config_kwargs: dict = {}
        # google_search grounding has no user_location knob (unlike the claude
        # and openai search tools), so the city rides in the system instruction
        instruction_parts = []
        if location:
            zip_suffix = f" {location.zip}" if location.zip else ""
            instruction_parts.append(f"The user is located in {location.city}, {location.state}{zip_suffix}.")
        if system:
            instruction_parts.append(system)
        if instruction_parts:
            config_kwargs["system_instruction"] = " ".join(instruction_parts)
        if schema:
            # response_json_schema takes standard JSON Schema (additionalProperties
            # et al); response_schema only accepts Google's OpenAPI-style subset
            config_kwargs["response_json_schema"] = schema
            config_kwargs["response_mime_type"] = "application/json"
        # MAX_WEB_SEARCHES=0 disables grounding outright (same lever as claude)
        web_search_enabled = (self.use_web_search if use_web_search is None else use_web_search) and self.max_web_searches > 0
        if web_search_enabled:
            config_kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]
        return types.GenerateContentConfig(**config_kwargs)

    @staticmethod
    def _state_name(batch) -> str:
        return batch.state.name if hasattr(batch.state, "name") else str(batch.state)

    @staticmethod
    def _extract_structured(response) -> dict | None:
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, dict):
            return parsed
        text = getattr(response, "text", "") or ""
        try:
            result = json.loads(text) if text else None
            return result if isinstance(result, dict) else None
        except (json.JSONDecodeError, ValueError):
            return None

    @staticmethod
    def _extract_citations(response) -> list[CitationContext]:
        candidates = getattr(response, "candidates", None) or []
        grounding = getattr(candidates[0], "grounding_metadata", None) if candidates else None
        if grounding is None:
            return []
        chunks = grounding.grounding_chunks or []
        citations: list[CitationContext] = []
        for support in grounding.grounding_supports or []:
            segment_text = (support.segment.text if support.segment else "") or ""
            for chunk_index in support.grounding_chunk_indices or []:
                web = chunks[chunk_index].web if chunk_index < len(chunks) else None
                if web is None or not web.uri:
                    continue
                citations.append(
                    CitationContext(
                        cited_text=segment_text,
                        title=web.title or "",
                        type="grounding",
                        # uri is a vertexaisearch.cloud.google.com redirect, not
                        # the real page; the chunk's domain (title falls back to
                        # it too) carries the actual source
                        url=web.uri,
                        domain=web.domain or web.title or None,
                    )
                )
        return citations

    @staticmethod
    def _extract_usage(response) -> dict | None:
        usage = getattr(response, "usage_metadata", None)
        if usage is None:
            return None
        candidates = getattr(response, "candidates", None) or []
        grounding = getattr(candidates[0], "grounding_metadata", None) if candidates else None
        searches = len((grounding.web_search_queries or []) if grounding else [])
        return {
            "input_tokens": usage.prompt_token_count or 0,
            "output_tokens": usage.candidates_token_count or 0,
            "server_tool_use": {"web_search_requests": searches},
        }
