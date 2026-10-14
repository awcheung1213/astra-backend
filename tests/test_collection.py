"""Collection's LLM boundary behaves purely: engines are canned fakes, no
provider call is ever made, and ingestion is a deterministic mapping from
canned answers to rows.

The pack-helper tests are DB-free. The two ingest tests need the database
(make test) but run inside ONE uncommitted transaction — locally the replica
resolves to the same session, so reads see the flushed rows and everything
rolls back on close (no fake batches left for the real poller to trip on)."""

import uuid
from types import SimpleNamespace

from sqlalchemy import select

from adapters.services.claude_service import ClaudeService
from constants.constants import UsageKind
from objects.batch import Batch
from objects.brand import Brand
from objects.market import Market
from objects.organization import Organization
from objects.prompt import Prompt
from objects.prompt_result import SENTIMENT_POSITIVE, PromptResult
from objects.query_response import QueryResponse
from schemas.provider_schemas import BatchHandle, QueryResult
from utils.database import Database, ReplicaDatabase
from utils.query_normalization import query_hash
from worker.tasks.collection import (
    RESPONSE_TRUNCATE_CHARS,
    SENTIMENT_PACK_SIZE,
    _build_sentiment_analysis_prompt,
    _categorize_into_sentiment_or_competitor_discovery_analysis,
    _chunk_prompt_results,
    _collect_sentiment_batch_outputs,
    _is_discovery_sample,
    _map_prompt_result_ids_to_structured_outputs,
    ingest_execution_batch,
    ingest_sentiment_batch,
)


def _pack_item(index, position=None, positive=False, top3=False, **extra) -> dict:
    return {"index": index, "brand_mention_position": position, "positive_sentiment": positive, "top_3_position": top3, **extra}


def _ids_with_sample_verdict(wanted: bool, count: int) -> list[str]:
    """Deterministically mine ids the 5% discovery sampler does/doesn't pick."""
    matches: list[str] = []
    for n in range(100_000):
        candidate = f"prompt-result-{n}"
        if _is_discovery_sample(candidate) is wanted:
            matches.append(candidate)
            if len(matches) == count:
                return matches
    raise AssertionError("no candidate ids found")


##############################
# PACK HELPERS (pure, DB-free)
##############################


def test_pack_items_map_back_by_echoed_index_and_bad_indexes_fail_alone():
    prompt_result_ids = ["pr-a", "pr-b", "pr-c"]
    structured = {"results": [_pack_item(0), _pack_item(2), _pack_item(7), _pack_item(None)]}
    pairs = _map_prompt_result_ids_to_structured_outputs(structured, prompt_result_ids)
    assert [prompt_result_id for prompt_result_id, _ in pairs] == ["pr-a", "pr-c"]


def test_competitors_are_extracted_only_from_discovery_packs():
    sentiment_output = {"results": [_pack_item(0, competitor_mentions=[{"name": "Sneaky Co", "position": 1}])]}
    discovery_output = {"results": [_pack_item(0, competitor_mentions=[{"name": "Rival Co", "position": 2}])]}
    sentiment_by_id, competitors_discovered = _collect_sentiment_batch_outputs(
        [
            (sentiment_output, ["pr-a"], UsageKind.SENTIMENT_ANALYSIS.value),
            (discovery_output, ["pr-b"], UsageKind.COMPETITOR_DISCOVERY.value),
        ]
    )
    assert set(sentiment_by_id) == {"pr-a", "pr-b"}
    assert competitors_discovered == ["Rival Co"]


def test_discovery_sampling_is_deterministic_and_routes_packs():
    sampled_id = _ids_with_sample_verdict(wanted=True, count=1)[0]
    assert all(_is_discovery_sample(sampled_id) for _ in range(3))

    unsampled_mentioned_id, unsampled_silent_id = _ids_with_sample_verdict(wanted=False, count=2)
    sampled_silent = SimpleNamespace(id=sampled_id, brand_mentioned=False)
    unsampled_mentioned = SimpleNamespace(id=unsampled_mentioned_id, brand_mentioned=True)
    unsampled_silent = SimpleNamespace(id=unsampled_silent_id, brand_mentioned=False)

    for_sentiment, for_discovery = _categorize_into_sentiment_or_competitor_discovery_analysis([sampled_silent, unsampled_mentioned, unsampled_silent])
    assert for_sentiment == [unsampled_mentioned]  # mentioned + unsampled
    assert for_discovery == [sampled_silent]  # sampled, mention or not
    # unsampled + silent rides no pack at all — the free parse finalizes it


def test_packs_chunk_at_pack_size():
    prompt_results = [SimpleNamespace(id=str(n)) for n in range(SENTIMENT_PACK_SIZE * 2 + 1)]
    packs = _chunk_prompt_results(prompt_results)
    assert [len(pack) for pack in packs] == [SENTIMENT_PACK_SIZE, SENTIMENT_PACK_SIZE, 1]


def test_sentiment_prompt_numbers_and_truncates_responses():
    long_answer = "x" * (RESPONSE_TRUNCATE_CHARS + 500)
    prompt = _build_sentiment_analysis_prompt("Acme", [SimpleNamespace(text="short answer"), SimpleNamespace(text=long_answer)])
    assert prompt.startswith("Brand: Acme")
    assert "### Response 0\nshort answer" in prompt
    assert "x" * RESPONSE_TRUNCATE_CHARS in prompt
    assert "x" * (RESPONSE_TRUNCATE_CHARS + 1) not in prompt


##############################
# INGEST (DB, one rolled-back transaction, fake engines)
##############################


def _seed_collection_rows(session) -> dict:
    organization = Organization(name=f"test-org-{uuid.uuid4()}")
    session.add(organization)
    brand = Brand(organization_id=organization.id, name=f"Brand {uuid.uuid4().hex[:8]}", website_url="https://example.com")
    prompt = Prompt(brand_id=brand.id, text="best dentist near me", category="local", is_active=True, tier="core")
    market = Market(city=f"Testville-{uuid.uuid4().hex[:8]}", state="CA")
    return {"organization": organization, "brand": brand, "prompt": prompt, "market": market}


async def _flush_seed(session) -> dict:
    seeded = _seed_collection_rows(session)
    session.add(seeded["organization"])
    await session.flush()
    session.add(seeded["brand"])
    await session.flush()
    session.add(seeded["prompt"])
    session.add(seeded["market"])
    await session.flush()
    return seeded


async def test_execution_ingest_maps_canned_answer_to_rows_without_provider_calls(monkeypatch):
    sentiment_submissions: list[dict] = []

    async def fake_submit_batch(self, query_requests):
        sentiment_submissions.append({"use_web_search": self.use_web_search, "requests": query_requests})
        return BatchHandle(provider="claude", batch_id=f"fake-{uuid.uuid4()}", status="in_progress")

    monkeypatch.setattr(ClaudeService, "submit_batch", fake_submit_batch)

    async with ReplicaDatabase().connection(), Database().connection():
        session = Database.session
        seeded = await _flush_seed(session)
        brand, prompt, market = seeded["brand"], seeded["prompt"], seeded["market"]
        canned_answer = f"Top picks:\n\n1. {brand.name} — excellent\n2. Someone Else — fine"

        execution_batch = Batch(  # in-memory only — ingest reads attrs, never the row
            provider="claude",
            batch_id="never-submitted",
            brand_id=None,
            status="ended",
            request_metadata={"0": {"query_hash": query_hash(prompt.text), "query_text": prompt.text, "prompt_id": prompt.id, "market_id": market.id, "subscribers": [{"brand_id": brand.id, "prompt_id": prompt.id}]}},
            kind=UsageKind.EXECUTE_PROMPTS,
        )
        await ingest_execution_batch(execution_batch, [QueryResult(custom_id="0", text=canned_answer, usage={"input_tokens": 100, "output_tokens": 50})])

        # corpus row: brand-less, verbatim
        query_response = (await session.execute(select(QueryResponse).where(QueryResponse.market_id == market.id))).scalars().one()
        assert query_response.text == canned_answer

        # the subscriber's derived view: mechanical mention detection, verbatim text
        prompt_result = (await session.execute(select(PromptResult).where(PromptResult.brand_id == brand.id))).scalars().one()
        assert prompt_result.brand_mentioned is True
        assert prompt_result.text == canned_answer
        assert prompt_result.query_response_id == query_response.id

        # the analysis call is pure: no web search, canned instructions, forced
        # structured output — the model can only map answers to the schema
        assert len(sentiment_submissions) == 1
        submission = sentiment_submissions[0]
        assert submission["use_web_search"] is False
        (pack_request,) = submission["requests"]
        assert pack_request.schema is not None
        assert pack_request.system is not None
        assert pack_request.cache_system is True
        assert f"### Response 0\n{canned_answer}" in pack_request.prompt

        sentiment_batch = (await session.execute(select(Batch).where(Batch.brand_id == brand.id))).scalars().one()
        assert sentiment_batch.kind == UsageKind.SENTIMENT_ANALYSIS
        assert sentiment_batch.request_metadata["0"]["prompt_result_ids"] == [prompt_result.id]
        # no commit: the transaction rolls back on close — nothing persists


async def test_sentiment_ingest_maps_pack_verdicts_mechanically():
    async with ReplicaDatabase().connection(), Database().connection():
        session = Database.session
        seeded = await _flush_seed(session)
        brand, prompt, market = seeded["brand"], seeded["prompt"], seeded["market"]

        # prose answer: the free list parser finds no rank, so the pack's
        # brand_mention_position must be the fallback
        query_response = await QueryResponse.create(query_hash=query_hash(prompt.text), query_text=prompt.text, market=market, engine="claude", text=f"{brand.name} is a fine choice.", raw={}, citations=[])
        prompt_result = await PromptResult.create(prompt_id=prompt.id, market_id=market.id, brand_id=brand.id, engine="claude", text=query_response.text, raw="{}", brand_cited=False, brand_mentioned=True, query_response_id=query_response.id)

        sentiment_batch = Batch(  # in-memory only
            provider="claude",
            batch_id="never-submitted",
            brand_id=brand.id,
            status="ended",
            request_metadata={"0": {"brand_id": brand.id, "prompt_result_ids": [prompt_result.id], "pack_kind": UsageKind.SENTIMENT_ANALYSIS.value}},
            kind=UsageKind.SENTIMENT_ANALYSIS,
        )
        pack_verdict = {"results": [_pack_item(0, position=2, positive=True, top3=True)]}
        await ingest_sentiment_batch(sentiment_batch, [QueryResult(custom_id="0", text="", structured=pack_verdict)])

        analyzed = await session.get(PromptResult, prompt_result.id)
        assert analyzed.rank == 2  # the pack's position, since the parser found no list
        assert analyzed.sentiment == SENTIMENT_POSITIVE
        assert analyzed.analyzed_at is not None  # analysis is final
        # no commit: rolls back on close
