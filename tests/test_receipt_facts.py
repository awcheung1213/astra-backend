"""Pure (no DB, no client fixture): the Your-tasks "recent" decisions list
and the change receipt's additions — proposed_at + a published page's
word/photo counts (utils/receipt_facts.py). Rows are SimpleNamespace fakes."""

from datetime import datetime, timedelta
from types import SimpleNamespace

from utils import receipt_facts

NOW = datetime(2026, 10, 14, 12, 0)
JARGON_ON_SCREEN = ("schema", "ld+json", "meta description", "change set", "prompt", "sweep", "queue", "workstream")


def _artifact(type="change_proposal", status="approved", content=None, evidence=None, id="a1", created_at=None, reviewed_at=None):
    return SimpleNamespace(id=id, type=type, status=status, content=content or {}, evidence=evidence or {}, created_at=created_at or NOW - timedelta(days=3), reviewed_at=reviewed_at)


def _change_set(id="cs1", status="applied", artifact_id="a1", reverted_by=None, operations=None, applied_at=None, created_at=None):
    operations = operations if operations is not None else [{"id": "op-0", "op": "set_field", "field": "title", "target": "https://reyes.example/about/", "value": "About Reyes Plumbing"}]
    return SimpleNamespace(id=id, status=status, artifact_id=artifact_id, reverted_by_change_set_id=reverted_by, operations=operations, applied_at=applied_at, created_at=created_at or NOW - timedelta(days=1))


def _sent_pitch(route="gmail", status="sent", **extra):
    send = {"status": status, "route": route, "to": "editor@austinnews.example", "sent_at": (NOW - timedelta(hours=1)).isoformat()}
    return _artifact("pitch", "approved", content={"where": "https://www.austinnews.example/best-plumbers", "send": send}, reviewed_at=NOW - timedelta(hours=2), **extra)


##############################
# DECISIONS
##############################


def test_decision_words():
    assert receipt_facts.decision(_artifact(status="rejected")) == "rejected"
    assert receipt_facts.decision(_artifact(status="auto_approved")) == "auto_approved"
    assert receipt_facts.decision(_artifact(status="approved")) == "approved"
    assert receipt_facts.decision(_sent_pitch()) == "sent"
    assert receipt_facts.decision(_sent_pitch(route="manual")) == "marked_sent"
    assert receipt_facts.decision(_sent_pitch(status="cancelled")) == "cancelled"
    assert receipt_facts.decision(_sent_pitch(status="scheduled")) == "approved"  # inside the undo window
    assert receipt_facts.decision(_sent_pitch(status="failed")) == "approved"


def test_decided_at_prefers_verdict_then_send_then_birth():
    assert receipt_facts.decided_at(_artifact(reviewed_at=NOW - timedelta(days=1))) == NOW - timedelta(days=1)
    assert receipt_facts.decided_at(_artifact(status="auto_approved", reviewed_at=None)) == NOW - timedelta(days=3)
    assert receipt_facts.decided_at(_sent_pitch()) == NOW - timedelta(hours=1)  # dated by the send


def test_titles_are_owner_words():
    titles = [
        receipt_facts.decision_title(_artifact(content={"display": {"title": "Give Google a short description of your homepage"}})),
        receipt_facts.decision_title(_artifact(content={}), _change_set()),
        receipt_facts.decision_title(_sent_pitch()),
        receipt_facts.decision_title(_artifact("pitch", content={"to_email": "hi@blog.example"})),
        receipt_facts.decision_title(_artifact("draft", content={"kind": "new", "title": "Emergency plumber in Austin"})),
        receipt_facts.decision_title(_artifact("draft", content={"kind": "refresh", "page": "https://reyes.example/water-heaters/"})),
        receipt_facts.decision_title(_artifact("calendar", content={"entries": [{}, {}, {}]})),
        receipt_facts.decision_title(_artifact("plan")),
        receipt_facts.decision_title(_artifact("reply", evidence={"rating": 2})),
        receipt_facts.decision_title(_artifact("gbp_touches", content={"posts": ["a", "b"]})),
        receipt_facts.decision_title(_artifact("review_ask_templates")),
    ]
    assert titles == [
        "Give Google a short description of your homepage",
        "Set the title Google shows for your about page",
        "Email to austinnews.example",
        "Email to blog.example",
        "New page: Emergency plumber in Austin",
        "A fresher version of your water heaters page",
        "3 new articles for next month",
        "Your plan for the next 3 months",
        "Reply to a 2-star review",
        "2 posts for your Google Business Profile",
        "Messages asking customers for reviews",
    ]
    for title in titles:
        assert not any(word in title.lower() for word in JARGON_ON_SCREEN)


def test_resulting_change_set_prefers_live_then_newest_forward_never_the_undo():
    live = _change_set("cs1", "applied", applied_at=NOW)
    assert receipt_facts.resulting_change_set([live]).id == "cs1"
    undone = _change_set("cs1", "reverted", reverted_by="cs2", applied_at=NOW - timedelta(days=1), created_at=NOW - timedelta(days=1))
    undo = _change_set("cs2", "applied", applied_at=NOW, created_at=NOW)
    assert receipt_facts.resulting_change_set([undo, undone]).id == "cs1"
    assert receipt_facts.resulting_change_set([_change_set("cs9", "pending")]).id == "cs9"
    assert receipt_facts.resulting_change_set([]) is None


def test_recent_entries_shape_order_filter_and_cap():
    fix = _artifact("change_proposal", "approved", content={"display": {"title": "Add a title to your homepage"}}, id="fix", reviewed_at=NOW - timedelta(days=2))
    rejected = _artifact("draft", "rejected", content={"title": "Plumbing tips"}, id="draft", reviewed_at=NOW - timedelta(days=1))
    pitch = _sent_pitch(id="pitch")
    brief = _artifact("brief", "auto_approved", id="brief", created_at=NOW)  # informational: never a decision
    superseded = _artifact("plan", "superseded", id="old-plan", reviewed_at=NOW)
    change_sets = [_change_set("cs1", "applied", artifact_id="fix", applied_at=NOW - timedelta(days=2))]
    entries = receipt_facts.recent_entries([fix, rejected, pitch, brief, superseded], change_sets)
    assert [entry["id"] for entry in entries] == ["pitch", "draft", "fix"]
    assert entries[0] == {"id": "pitch", "type": "pitch", "title": "Email to austinnews.example", "decision": "sent", "decided_at": (NOW - timedelta(hours=1)).isoformat() + "+00:00", "change_set_id": None}
    assert entries[1]["decision"] == "rejected" and entries[1]["change_set_id"] is None
    assert entries[2]["change_set_id"] == "cs1" and entries[2]["decision"] == "approved"
    many = [_artifact(id=f"a{index}", reviewed_at=NOW - timedelta(hours=index)) for index in range(15)]
    assert len(receipt_facts.recent_entries(many, [])) == 10


##############################
# RECEIPT ADDITIONS
##############################


def test_word_and_photo_counts_markdown_and_html():
    body = (
        "# Emergency plumber in Austin\n\n"
        "We fix **burst pipes** fast. [Call us](https://reyes.example/contact) today.\n\n"
        "![A van outside a house](https://reyes.example/van.jpg)\n\n"
        "1. Leaks\n2. Drains\n- Water heaters\n\n"
        '<p>Open <em>24/7</em>.</p><img src="team.jpg" alt="our team"> <IMG src=x>\n'
        "![logo][brand]\n"
        "<script>var tracking = 1;</script>\n"
        "---\n"
    )
    assert receipt_facts.photo_count(body) == 4
    # Emergency plumber in Austin (4) + We fix burst pipes fast. Call us today. (8) + Leaks Drains Water heaters (4) + Open 24/7. (2)
    assert receipt_facts.word_count(body) == 18
    assert receipt_facts.word_count(None) == 0 and receipt_facts.photo_count("") == 0


def test_page_facts_only_for_a_published_page():
    publish = _change_set(operations=[{"id": "op-0", "op": "publish_page", "target": None, "value": {"title": "Water heaters", "meta_description": "x", "body_markdown": "Tank and tankless installs. ![tank](t.jpg)"}}])
    assert receipt_facts.page_facts(publish) == {"word_count": 4, "photo_count": 1}
    assert receipt_facts.page_facts(_change_set()) is None
    rewrite = _change_set(operations=[{"id": "op-0", "op": "update_page", "target": "https://reyes.example/a/", "value": {"body_markdown": "words here"}}])
    assert receipt_facts.page_facts(rewrite) is None
    assert receipt_facts.page_facts(_change_set(operations=[{"op": "publish_page", "value": None}])) == {"word_count": 0, "photo_count": 0}


def test_receipt_additions():
    artifact = _artifact(created_at=datetime(2026, 10, 10, 9, 30))
    assert receipt_facts.receipt_additions(_change_set(), artifact) == {"proposed_at": "2026-10-10T09:30:00+00:00"}
    publish = _change_set(operations=[{"op": "publish_page", "value": {"body_markdown": "One two three"}}])
    assert receipt_facts.receipt_additions(publish, artifact) == {"proposed_at": "2026-10-10T09:30:00+00:00", "page_facts": {"word_count": 3, "photo_count": 0}}
    assert receipt_facts.receipt_additions(_change_set(), None) == {"proposed_at": None}


##############################
# WIRING (monkeypatched — no DB)
##############################


def _returns(value):
    async def fake(*args, **kwargs):
        return value

    return fake


async def test_approvals_endpoint_adds_recent_and_keeps_existing_keys(monkeypatch):
    import api.reads as reads

    fix = _artifact("change_proposal", "auto_approved", content={"display": {"title": "Add a title to your homepage"}}, id="fix", reviewed_at=NOW)
    monkeypatch.setattr(reads.Brand, "get", _returns(SimpleNamespace(id="b1")))
    monkeypatch.setattr(reads.Task, "for_brand", _returns([]))
    monkeypatch.setattr(reads.Artifact, "pending_for_brand", _returns([]))
    monkeypatch.setattr(receipt_facts.Artifact, "decided_for_brand", _returns([fix]))
    monkeypatch.setattr(receipt_facts.ChangeSet, "for_artifacts", _returns([_change_set("cs1", "applied", artifact_id="fix", applied_at=NOW)]))
    payload = await reads.approvals("b1")
    assert set(payload) == {"pending", "waiting_on_you", "recent"}
    assert payload["recent"] == [{"id": "fix", "type": "change_proposal", "title": "Add a title to your homepage", "decision": "auto_approved", "decided_at": NOW.isoformat() + "+00:00", "change_set_id": "cs1"}]


async def test_recent_queries_read_what_they_say(monkeypatch):
    from sqlalchemy.dialects import postgresql

    from objects.artifact import Artifact
    from objects.change_set import ChangeSet
    from utils.database import Database

    statements: list = []

    class Capture:
        async def execute(self, statement):
            statements.append(statement)
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [], first=lambda: None))

    monkeypatch.setattr(Database, "get_session", lambda self: Capture())
    await Artifact.decided_for_brand("b1", statuses=receipt_facts.DECIDED_STATUSES, exclude_types={"brief"}, limit=10)
    await ChangeSet.for_artifacts(["a1", None])
    assert await ChangeSet.for_artifacts([]) == []
    decided, by_artifacts = (" ".join(str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})).split()) for statement in statements)
    assert "artifacts.status IN ('approved', 'auto_approved', 'rejected')" in decided and "artifacts.type NOT IN ('brief')" in decided
    assert "ORDER BY coalesce(artifacts.reviewed_at, artifacts.created_at) DESC" in decided and "LIMIT 10" in decided
    assert "change_sets.artifact_id IN ('a1')" in by_artifacts
