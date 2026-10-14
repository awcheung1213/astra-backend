"""BUG-017 (DEC-008=A): sentiment-batch cost_events carry the brand's org;
execution (corpus) batches stay org-NULL — pure unit test, no DB, no client fixture."""

from types import SimpleNamespace

from constants.constants import UsageKind
from schemas.provider_schemas import QueryResult
from utils.database import Database
from worker.tasks import collection


class FakeSession:
    async def commit(self) -> None:
        pass


class FakeEngine:
    TERMINAL_STATUSES = {"ended"}
    model = "claude-sonnet-4-5"

    async def get_batch_status(self, batch_id: str) -> str:
        return "ended"

    async def get_batch_results(self, batch_id: str) -> list[QueryResult]:
        return [QueryResult(custom_id="0", text="", usage={"input_tokens": 100, "output_tokens": 10})]


async def test_poll_attributes_sentiment_spend_to_the_brands_org_and_keeps_corpus_org_null(monkeypatch):
    sentiment = SimpleNamespace(id="b-sent", batch_id="p1", provider="claude", kind=UsageKind.SENTIMENT_ANALYSIS.value, brand_id="brand-1", task_id=None)
    execution = SimpleNamespace(id="b-exec", batch_id="p2", provider="claude", kind=UsageKind.EXECUTE_PROMPTS.value, brand_id=None, task_id=None)
    cost_events: list[dict] = []
    ingested: list[str] = []

    async def fake_unprocessed(kind=None):
        return [sentiment, execution]

    async def fake_create(**kwargs):
        cost_events.append(kwargs)

    async def fake_brand_get(id):
        return SimpleNamespace(id=id, organization_id="org-1")

    async def fake_ingest(batch, results):
        ingested.append(batch.id)

    async def fake_mark_processed(id, status):
        pass

    monkeypatch.setattr(collection.Batch, "unprocessed", staticmethod(fake_unprocessed))
    monkeypatch.setattr(collection.Batch, "mark_processed", staticmethod(fake_mark_processed))
    monkeypatch.setattr(collection.CostEvent, "create", staticmethod(fake_create))
    monkeypatch.setattr(collection.Brand, "get", staticmethod(fake_brand_get))
    monkeypatch.setattr(collection, "initialize_adapter", lambda adapter: FakeEngine())
    monkeypatch.setattr(collection, "ingest_sentiment_batch", fake_ingest)
    monkeypatch.setattr(collection, "ingest_execution_batch", fake_ingest)

    async def no_scoring(brand_ids):
        return None  # event-driven scoring has its own tests

    monkeypatch.setattr(collection, "score_brands_whose_answers_landed", no_scoring)
    monkeypatch.setattr(Database, "get_session", lambda self: FakeSession())

    await collection.run_poll_batches()

    by_op = {event["operation"]: event for event in cost_events}
    assert by_op[UsageKind.SENTIMENT_ANALYSIS.value]["organization_id"] == "org-1"
    assert by_op[UsageKind.SENTIMENT_ANALYSIS.value]["brand_id"] == "brand-1"
    assert by_op[UsageKind.EXECUTE_PROMPTS.value]["organization_id"] is None
    # BUG-004: batch-ingest cost stays in the ingest transaction (no own_transaction)
    assert all(not event.get("own_transaction") for event in cost_events)
    assert ingested == ["b-sent", "b-exec"]
