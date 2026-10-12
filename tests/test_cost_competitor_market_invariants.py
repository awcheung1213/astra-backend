"""BUG-004 (cost_events survive a task rollback), BUG-005 (competitor source
written, human rows never revised by the system), BUG-016 (markets unique on
lower(city), lower(state)) — pure unit tests, no DB, no client fixture."""

import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateIndex

from adapters.agents import claude_agent
from adapters.agents.claude_agent import ClaudeAgent
from api import competitors as competitors_api
from objects.competitor import SOURCE_AI_BOOTSTRAP, SOURCE_DISCOVERED, SOURCE_USER, Competitor
from objects.cost_event import CostEvent
from objects.market import Market
from schemas.competitor_schemas import CompetitorAddRequest
from utils.database import Database

REPO = Path(__file__).resolve().parent.parent


##############################
# BUG-004: cost events commit on their own session
##############################


class FakeSession:
    def __init__(self) -> None:
        self.added: list = []
        self.committed: list = []
        self.closed = False

    def add(self, row) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        self.committed.extend(self.added)

    async def rollback(self) -> None:
        self.added = []

    async def flush(self) -> None:
        pass

    async def refresh(self, row) -> None:
        pass

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *exc) -> None:
        self.closed = True


@pytest.fixture
def sessions(monkeypatch):
    """ambient = the task's ContextVar session (rolled back on failure); own = the fresh one."""
    ambient, own = FakeSession(), FakeSession()
    monkeypatch.setattr(Database, "get_session", lambda self: ambient)
    monkeypatch.setattr(Database, "create_session", lambda self: own)
    return SimpleNamespace(ambient=ambient, own=own)


async def test_own_transaction_cost_event_survives_ambient_rollback(sessions):
    event = await CostEvent.create(adapter="claude", operation="prompt_research", task_id="t1", model="claude-opus-5", input_tokens=10, usd=0.5, own_transaction=True)
    await sessions.ambient.rollback()  # task_lifecycle's except branch
    assert sessions.own.committed == [event] and sessions.own.closed
    assert sessions.ambient.added == []


async def test_default_cost_event_stays_in_the_ambient_transaction(sessions):
    # batch ingest: atomic with mark_processed, so a failed ingest re-polls and re-records once
    event = await CostEvent.create(adapter="claude", operation="execution", usd=0.1)
    assert sessions.ambient.added == [event] and sessions.own.added == []


async def test_run_agent_meters_every_turn_on_its_own_transaction(monkeypatch):
    calls: list[dict] = []

    async def fake_create(**kwargs):
        calls.append(kwargs)

    async def fake_turn(self, messages, system=None, tools=None, **kwargs):
        usage = SimpleNamespace(input_tokens=100, output_tokens=20, server_tool_use=None)
        return SimpleNamespace(usage=usage, model="claude-opus-5", stop_reason="end_turn", content=[SimpleNamespace(type="text", text="[]")])

    monkeypatch.setattr(claude_agent.CostEvent, "create", staticmethod(fake_create))
    monkeypatch.setattr(ClaudeAgent, "agent_turn", fake_turn)
    await ClaudeAgent().run_agent(system="s", prompt="p", tools=[], handlers={}, operation="prompt_research", brand_id="b1", task_id="t1")
    assert len(calls) == 1 and calls[0]["own_transaction"] is True and calls[0]["task_id"] == "t1"


##############################
# BUG-005: competitor source
##############################


def test_upsert_revises_only_machine_rows_unless_user():
    assert Competitor.upsert_revises(SOURCE_AI_BOOTSTRAP, SOURCE_DISCOVERED)
    assert Competitor.upsert_revises(SOURCE_DISCOVERED, SOURCE_AI_BOOTSTRAP)
    assert Competitor.upsert_revises(SOURCE_AI_BOOTSTRAP, SOURCE_USER)
    assert Competitor.upsert_revises(SOURCE_USER, SOURCE_USER)
    assert not Competitor.upsert_revises(SOURCE_USER, SOURCE_DISCOVERED)
    assert not Competitor.upsert_revises(SOURCE_USER, SOURCE_AI_BOOTSTRAP)


@pytest.fixture
def competitor_store(monkeypatch):
    """Competitor.upsert over a fake lookup; records what update/create would write."""
    store = SimpleNamespace(existing=None, updates=[], creates=[])

    class FakeResult:
        def scalars(self):
            return SimpleNamespace(first=lambda: store.existing)

    class FakeReplica:
        async def execute(self, statement):
            return FakeResult()

    async def fake_update(**kwargs):
        store.updates.append(kwargs)
        return store.existing

    async def fake_create(**kwargs):
        store.creates.append(kwargs)
        return Competitor(id="new", **kwargs)

    # ReplicaDatabase() resolves to the Database singleton, so patch the base
    monkeypatch.setattr(Database, "get_session", lambda self: FakeReplica())
    monkeypatch.setattr(Competitor, "update", staticmethod(fake_update))
    monkeypatch.setattr(Competitor, "create", staticmethod(fake_create))
    return store


async def test_new_competitor_carries_the_callers_source(competitor_store):
    await Competitor.upsert("b1", "Acme Co", source=SOURCE_DISCOVERED)
    assert competitor_store.creates[0]["source"] == SOURCE_DISCOVERED and competitor_store.creates[0]["normalized_name"] == "acme"


async def test_machine_upsert_never_respells_a_human_row(competitor_store):
    competitor_store.existing = Competitor(id="c1", brand_id="b1", name="ACME", normalized_name="acme", source=SOURCE_USER)
    await Competitor.upsert("b1", "Acme Co", source=SOURCE_DISCOVERED)
    update = competitor_store.updates[0]
    assert update["name"] is None and update["source"] is None


async def test_machine_upsert_respells_its_own_row_and_keeps_source(competitor_store):
    competitor_store.existing = Competitor(id="c1", brand_id="b1", name="acme", normalized_name="acme", source=SOURCE_AI_BOOTSTRAP)
    await Competitor.upsert("b1", "Acme Co", source=SOURCE_DISCOVERED)
    update = competitor_store.updates[0]
    assert update["name"] == "Acme Co" and update["source"] is None


async def test_user_upsert_claims_a_machine_row(competitor_store):
    competitor_store.existing = Competitor(id="c1", brand_id="b1", name="acme", normalized_name="acme", source=SOURCE_DISCOVERED)
    await Competitor.upsert("b1", "ACME", source=SOURCE_USER, is_excluded=False)
    update = competitor_store.updates[0]
    assert update["name"] == "ACME" and update["source"] == SOURCE_USER and update["is_excluded"] is False


async def test_client_add_writes_source_user(monkeypatch):
    seen: dict = {}

    async def fake_brand_get(brand_id):
        return SimpleNamespace(id=brand_id, name="Evergreen")

    async def fake_upsert(brand_id, name, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(id="c1", name=name, is_active=False)

    monkeypatch.setattr(competitors_api.Brand, "get", staticmethod(fake_brand_get))
    monkeypatch.setattr(competitors_api.Competitor, "upsert", staticmethod(fake_upsert))
    await competitors_api.add_competitor("b1", CompetitorAddRequest(name="Acme"))
    assert seen["source"] == SOURCE_USER


def test_every_competitor_upsert_call_site_passes_its_true_source():
    expected = {"api/competitors.py": {"SOURCE_USER"}, "worker/tasks/research.py": {"SOURCE_AI_BOOTSTRAP"}, "worker/tasks/collection.py": {"SOURCE_DISCOVERED"}}
    for path, sources in expected.items():
        tree = ast.parse((REPO / path).read_text())
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and ast.unparse(node.func) == "Competitor.upsert"]
        assert calls, path
        assert {ast.unparse(keyword.value) for call in calls for keyword in call.keywords if keyword.arg == "source"} == sources, path
        assert all(any(keyword.arg == "source" for keyword in call.keywords) for call in calls), path


##############################
# BUG-016: markets unique on lower(city), lower(state)
##############################


def test_markets_unique_index_is_case_insensitive():
    index = next(index for index in Market.__table__.indexes if index.name == "uq_markets_lower_city_state")
    ddl = str(CreateIndex(index).compile(dialect=postgresql.dialect()))
    assert index.unique and "lower(city), lower(state)" in ddl


def _load_migration():
    path = next((REPO / "alembic" / "versions").glob("*_310ab26709e8_*.py"))
    spec = importlib.util.spec_from_file_location("markets_lower_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_migration_is_the_single_head_and_swaps_the_constraint():
    migration = _load_migration()
    assert migration.revision == "310ab26709e8" and migration.down_revision == "9f83fec900f7"
    down_revisions = [ast.literal_eval(line.split("=", 1)[1].strip()) for path in (REPO / "alembic" / "versions").glob("*.py") for line in path.read_text().splitlines() if line.startswith("down_revision")]
    assert "310ab26709e8" not in down_revisions and down_revisions.count("9f83fec900f7") == 1

    ops: list[tuple] = []
    migration.op = SimpleNamespace(
        create_index=lambda name, table, columns, unique=False: ops.append(("create_index", name, [str(column) for column in columns], unique)),
        drop_constraint=lambda name, table, type_=None: ops.append(("drop_constraint", name, type_)),
        create_unique_constraint=lambda name, table, columns: ops.append(("create_unique_constraint", name, columns)),
        drop_index=lambda name, table_name=None: ops.append(("drop_index", name)),
    )
    migration.upgrade()
    assert ops == [("create_index", "uq_markets_lower_city_state", ["lower(city)", "lower(state)"], True), ("drop_constraint", "markets_city_state_key", "unique")]
    ops.clear()
    migration.downgrade()
    assert ops == [("create_unique_constraint", "markets_city_state_key", ["city", "state"]), ("drop_index", "uq_markets_lower_city_state")]


async def test_case_variant_race_refetches_the_winner(monkeypatch):
    winner = Market(id="m1", city="Mesa", state="AZ")
    lookups = iter([None, winner])

    async def fake_lookup(city, state):
        return next(lookups)

    class Savepoint:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            raise IntegrityError("INSERT", {}, Exception("duplicate key value violates unique constraint uq_markets_lower_city_state"))

    session = SimpleNamespace(begin_nested=lambda: Savepoint(), add=lambda row: None)
    monkeypatch.setattr(Market, "_by_city_state", staticmethod(fake_lookup))
    monkeypatch.setattr(Database, "get_session", lambda self: session)
    assert await Market.get_or_create_city("mesa", "az") is winner
