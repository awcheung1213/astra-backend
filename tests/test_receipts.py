"""Pure (no DB, no client fixture): the owner-language receipts — issue
status derivation, the done-for-you feed, change headlines + plain
before/after, and the fix lifecycle (an issue closes when its fix goes live,
reopens on undo; marco-review R5). Collaborators are fakes/monkeypatched."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import worker.tasks.changes as changes
import worker.tasks.site_audit as site_audit
from utils import receipts

NOW = datetime(2026, 10, 12, 12, 0)
CARD = {"@context": "https://schema.org", "@type": "LocalBusiness", "name": "Reyes Plumbing & Drain", "description": "Family plumbers serving Austin.", "telephone": "512-555-0100"}
PAGE = "<p>We fix pipes fast.</p>"
WITH_CARD = f'{PAGE}\n<script type="application/ld+json">{json.dumps(CARD)}</script>'
JARGON_ON_SCREEN = ("schema", "ld+json", "meta description", "change set", "prompt", "sweep", "queue", "workstream", "<script", "<p>")


def _finding(kind="missing_schema", url="https://reyes.example/", status="open", detail=None, **extra):
    return SimpleNamespace(
        id=extra.get("id", "f1"), kind=kind, severity=extra.get("severity", "warn"), summary=extra.get("summary", "raw summary"), detail={"url": url, **(detail or {})}, status=status, created_at=extra.get("created_at", NOW - timedelta(days=3)), resolved_at=extra.get("resolved_at")
    )


def _proposal(status="pending_review", id="a1", finding_id="f1", operations=True, display_title=None, created_at=None, reviewed_at=None):
    content = {"finding_id": finding_id, "kind": "missing_schema"}
    if operations:
        content["operations"] = [{"field": "content", "url": "https://reyes.example/", "value": WITH_CARD}]
    if display_title:
        content["display"] = {"title": display_title}
    return SimpleNamespace(id=id, type="change_proposal", status=status, content=content, evidence={}, created_at=created_at or NOW - timedelta(days=2), reviewed_at=reviewed_at)


def _change_set(id="cs1", status="applied", artifact_id="a1", reverted_by=None, operations=None, applied_at=None, created_at=None):
    operations = operations if operations is not None else [{"id": "op-0", "op": "set_field", "field": "content", "target": "https://reyes.example/", "value": WITH_CARD, "previous_value": PAGE, "completed_at": "x"}]
    return SimpleNamespace(id=id, brand_id="b1", status=status, artifact_id=artifact_id, reverted_by_change_set_id=reverted_by, operations=operations, applied_at=applied_at or (NOW - timedelta(days=1) if status in ("applied", "reverted") else None), created_at=created_at or NOW - timedelta(days=1))


##############################
# ISSUES
##############################


def test_open_issue_without_a_proposal_is_yellow_and_plain():
    entry = receipts.issue_entry(_finding(kind="missing_meta_description"), [], {})
    assert (entry["status"], entry["dot"], entry["status_label"]) == ("open", "yellow", "Not fixed yet")
    assert entry["title"] == "Google shows no description for your homepage"
    assert entry["page_label"] == "Homepage" and entry["page_url"] == "https://reyes.example/"
    assert entry["artifact_id"] is None and entry["change_set_id"] is None


def test_pending_proposal_means_fix_waiting_with_its_artifact():
    entry = receipts.issue_entry(_finding(url="https://reyes.example/about/"), [_proposal()], {})
    assert (entry["status"], entry["dot"], entry["status_label"], entry["artifact_id"]) == ("fix_waiting", "yellow", "Fix ready, waiting for your OK", "a1")
    assert entry["title"] == "Your about page has no business card for search engines" and entry["page_label"] == "About page"


def test_live_fix_is_green_fix_live_even_for_rows_closed_before_close_on_apply():
    for finding_status in ("fixed", "open"):  # open = a fix that went live before this feature existed
        entry = receipts.issue_entry(_finding(status=finding_status), [_proposal(status="approved")], {"a1": [_change_set()]})
        assert (entry["status"], entry["dot"], entry["status_label"], entry["change_set_id"], entry["artifact_id"]) == ("fix_live", "green", "Fixed, live on your site", "cs1", "a1")


def test_fixed_without_our_change_is_plain_fixed():
    entry = receipts.issue_entry(_finding(status="fixed", resolved_at=NOW), [], {})
    assert (entry["status"], entry["dot"], entry["status_label"]) == ("fixed", "green", "Fixed")
    assert entry["status_reason"] == "This problem no longer shows up on your site."


def test_agent_skip_is_grey_cant_fix_with_the_reason_in_plain_words():
    skipped = _finding(kind="missing_meta_description", detail={"skipped": {"reason": "Core WordPress has no meta description field; install Yoast or Rank Math so the excerpt renders as <meta name=description>."}})
    entry = receipts.issue_entry(skipped, [], {})
    assert (entry["status"], entry["dot"], entry["status_label"]) == ("cant_fix", "grey", "We can't fix this automatically")
    assert entry["status_reason"] == "Your site needs a free SEO plugin before we can set this."


def test_unfixable_kinds_say_why_we_cant():
    entry = receipts.issue_entry(_finding(kind="slow_page", detail={"elapsed_seconds": 4.21}, severity="serious"), [], {})
    assert entry["status"] == "cant_fix" and "web host" in entry["status_reason"]
    assert entry["title"] == "Your homepage is slow to load (4.2 seconds)"
    crawler = receipts.issue_entry(_finding(kind="blocked_ai_crawler", url="https://reyes.example/robots.txt", detail={"crawler": "GPTBot"}), [], {})
    assert crawler["title"] == "Your site blocks ChatGPT from reading it" and crawler["page_label"] == "Whole site"


def test_undone_fix_reopens_as_you_undid_it():
    chain = {"a1": [_change_set(status="reverted", reverted_by="cs2"), _change_set(id="cs2", status="applied")]}
    entry = receipts.issue_entry(_finding(), [_proposal(status="approved")], chain)
    assert (entry["status"], entry["status_label"], entry["change_set_id"]) == ("open", "You undid our fix", None)


def test_fix_that_did_not_take_says_so_while_live():
    entry = receipts.issue_entry(_finding(detail={"fix_did_not_take": {"at": "x"}, "resolved_by_change_set_id": "cs1"}), [_proposal(status="approved")], {"a1": [_change_set()]})
    assert (entry["status"], entry["status_label"]) == ("open", "Still showing after our fix")


def test_approved_but_not_applied_and_rejected_and_hand_applied():
    approved = receipts.issue_entry(_finding(), [_proposal(status="auto_approved")], {"a1": [_change_set(status="applying")]})
    assert approved["status_label"] == "Fix approved, going live shortly"
    rejected = receipts.issue_entry(_finding(), [_proposal(status="rejected")], {})
    assert rejected["status_label"] == "You turned down our fix"
    prose = receipts.issue_entry(_finding(), [_proposal(status="approved", operations=False)], {})
    assert prose["status_reason"] == "This one has to be made by hand on your site."


def test_issues_order_open_first_then_most_recently_resolved():
    entries = [
        {"status": "fixed", "resolved_at": "2026-10-01T00:00:00+00:00", "found_at": "a", "severity": "warn", "id": "old-fix"},
        {"status": "cant_fix", "resolved_at": None, "found_at": "2026-10-05", "severity": "serious", "id": "cant"},
        {"status": "open", "resolved_at": None, "found_at": "2026-10-04", "severity": "warn", "id": "open-warn"},
        {"status": "open", "resolved_at": None, "found_at": "2026-10-01", "severity": "serious", "id": "open-serious"},
        {"status": "fix_live", "resolved_at": "2026-10-10T00:00:00+00:00", "found_at": "a", "severity": "warn", "id": "new-fix"},
        {"status": "fix_waiting", "resolved_at": None, "found_at": "2026-10-02", "severity": "warn", "id": "waiting"},
    ]
    assert [entry["id"] for entry in receipts.order_issues(entries)] == ["waiting", "open-serious", "open-warn", "cant", "new-fix", "old-fix"]
    assert len(receipts.order_issues(entries, limit=2)) == 2


def test_no_issue_copy_leaks_jargon():
    kinds = ["missing_title", "missing_meta_description", "missing_h1", "missing_schema", "slow_page", "broken_link", "blocked_ai_crawler", "missing_sitemap"]
    for kind in kinds:
        entry = receipts.issue_entry(_finding(kind=kind, url="https://reyes.example/services/"), [], {})
        text = " ".join(str(entry[key] or "") for key in ("title", "why_it_matters", "status_label", "status_reason")).lower()
        assert not any(term in text for term in JARGON_ON_SCREEN), (kind, text)


def test_plain_skip_reason_keeps_plain_text_and_replaces_jargon():
    assert receipts.plain_skip_reason("The page returns 404, so there is nothing to edit.") == "We couldn't open this page to fix it."
    assert receipts.plain_skip_reason("Your contact page is a form we can't edit.") == "Your contact page is a form we can't edit."
    assert receipts.plain_skip_reason("Needs canonical rework across templates.") == receipts.SKIP_REASON_FALLBACK
    assert receipts.plain_skip_reason("") is None


##############################
# FIX LIFECYCLE
##############################


def test_live_fix_follows_undo_and_redo_parity():
    original = _change_set(id="o", status="applied")
    assert receipts.live_fix_change_set_id([original]) == "o"
    original.status, original.reverted_by_change_set_id = "reverted", "r1"
    undo = _change_set(id="r1", status="pending")
    assert receipts.live_fix_change_set_id([original, undo]) is None  # Undo clicked: no longer counted live
    undo.status = "applied"
    assert receipts.live_fix_change_set_id([original, undo]) is None
    undo.status, undo.reverted_by_change_set_id = "reverted", "r2"
    redo = _change_set(id="r2", status="applied")
    assert receipts.live_fix_change_set_id([original, undo, redo]) == "r2"  # an undo of the undo is forward again


def test_transition_resolves_open_and_reopens_only_what_this_fix_closed():
    assert receipts.finding_fix_transition("open", {}, "cs1", {"cs1"}) == "resolve"
    assert receipts.finding_fix_transition("fixed", {"resolved_by_change_set_id": "cs1"}, "cs1", {"cs1"}) is None  # idempotent
    assert receipts.finding_fix_transition("fixed", {"resolved_by_change_set_id": "cs1"}, None, {"cs1", "cs2"}) == "reopen"
    assert receipts.finding_fix_transition("fixed", {}, None, {"cs1"}) is None  # the audit closed it, not us
    assert receipts.finding_fix_transition("open", {}, None, {"cs1"}) is None  # already open
    assert receipts.finding_fix_transition("dismissed", {}, "cs1", {"cs1"}) is None
    assert receipts.reopened_detail({"url": "u", "resolved_by_change_set_id": "cs1", "fix_did_not_take": {}}) == {"url": "u"}
    assert receipts.resolved_detail({"url": "u", "fix_did_not_take": {}}, "cs9") == {"url": "u", "resolved_by_change_set_id": "cs9"}


class _FakeFindings:
    """Stands in for the findings table: by_ids_for_update + set_lifecycle."""

    def __init__(self, rows):
        self.rows = {row.id: row for row in rows}
        self.writes: list = []

    async def by_ids_for_update(self, ids):
        return [self.rows[finding_id] for finding_id in ids if finding_id in self.rows]

    async def set_lifecycle(self, finding, status, detail):
        finding.status, finding.detail = status, detail
        finding.resolved_at = NOW if status == "fixed" else None
        self.writes.append((finding.id, status))


def _patch_sync(monkeypatch, artifact, chain, findings):
    class _Session:
        async def get(self, model, id):
            return artifact if id == artifact.id else None

    monkeypatch.setattr(changes.Database, "get_session", lambda self: _Session())

    async def for_artifact(artifact_id):
        return chain

    monkeypatch.setattr(changes.ChangeSet, "for_artifact", for_artifact)
    monkeypatch.setattr(changes.Finding, "by_ids_for_update", findings.by_ids_for_update)
    monkeypatch.setattr(changes.Finding, "set_lifecycle", findings.set_lifecycle)


async def test_apply_closes_the_issue_undo_reopens_it_and_reruns_are_noops(monkeypatch):
    finding = _finding(id="f1")
    findings = _FakeFindings([finding])
    original = _change_set(id="o", status="applied")
    chain = [original]
    _patch_sync(monkeypatch, _proposal(status="approved"), chain, findings)

    assert await changes.sync_findings_with_fix("a1") == [("f1", "resolve")]
    assert finding.status == "fixed" and finding.detail["resolved_by_change_set_id"] == "o" and finding.resolved_at == NOW
    assert await changes.sync_findings_with_fix("a1") == []  # idempotent

    original.status, original.reverted_by_change_set_id = "reverted", "r1"
    chain.append(_change_set(id="r1", status="applied"))
    assert await changes.sync_findings_with_fix("a1") == [("f1", "reopen")]
    assert finding.status == "open" and finding.resolved_at is None and "resolved_by_change_set_id" not in finding.detail
    assert await changes.sync_findings_with_fix("a1") == []
    assert findings.writes == [("f1", "fixed"), ("f1", "open")]


async def test_non_fix_artifacts_touch_no_findings(monkeypatch):
    draft = SimpleNamespace(id="d1", type="draft", content={"title": "Drain cleaning"})
    findings = _FakeFindings([_finding()])
    _patch_sync(monkeypatch, draft, [_change_set(artifact_id="d1")], findings)
    assert await changes.sync_findings_with_fix("d1") == [] and findings.writes == []


async def test_audit_reopens_a_closed_by_fix_issue_instead_of_a_twin(monkeypatch):
    fixed = _finding(id="f-fixed", status="fixed", detail={"resolved_by_change_set_id": "cs1"})
    reopened, created, tasks = [], [], []

    async def _async(value):
        return value

    async def crawl(base_url):
        return [{"kind": "missing_schema", "severity": "warn", "summary": "s", "detail": {"url": "https://reyes.example/"}}]

    async def reopen_after_fix(id):
        reopened.append(id)

    async def create(**fields):
        created.append(fields)

    async def create_task(**fields):
        tasks.append(fields)

    async def none(*args, **kwargs):
        return None

    monkeypatch.setattr(site_audit.Brand, "get", lambda brand_id: _async(SimpleNamespace(id="b1", website_url="https://reyes.example")))
    monkeypatch.setattr(site_audit.Site, "by_brand", none)
    monkeypatch.setattr(site_audit, "_crawl", crawl)
    monkeypatch.setattr(site_audit.Finding, "open_for_brand", lambda brand_id: _async([]))
    monkeypatch.setattr(site_audit.Finding, "fixed_by_change_for_brand", lambda brand_id: _async([fixed]))
    monkeypatch.setattr(site_audit.Finding, "reopen_after_fix", reopen_after_fix)
    monkeypatch.setattr(site_audit.Finding, "create", create)
    monkeypatch.setattr(site_audit, "create_task", create_task)
    monkeypatch.setattr(site_audit, "queue_first_plan_when_ready", none)
    result = await site_audit.run_site_audit("b1")
    assert reopened == ["f-fixed"] and created == [] and tasks == []  # no twin, no weekly re-proposal
    assert result["created"] == 0


##############################
# CHANGES
##############################


def test_business_card_append_reads_as_an_invisible_card_with_plain_words():
    (change,) = receipts.change_details(_change_set())
    assert change["what"] == "Added a business card for search engines" and change["before"] is None and change["visible_to_visitors"] is False
    assert change["after"] == "Reyes Plumbing & Drain. Family plumbers serving Austin. Phone: 512-555-0100."
    assert change["page_label"] == "Homepage" and change["page_url"] == "https://reyes.example/"


def test_undo_of_a_card_reads_as_removed():
    undo = _change_set(id="r1", operations=[{"id": "op-0", "op": "set_field", "field": "content", "target": "https://reyes.example/", "value": PAGE, "previous_value": WITH_CARD, "completed_at": "x"}])
    (change,) = receipts.change_details(undo)
    assert change["what"] == "Removed the business card for search engines" and change["after"] is None and "Reyes Plumbing" in change["before"]


def test_field_changes_give_short_plain_before_and_after():
    long_title = "Emergency plumbing " * 20
    change_set = _change_set(
        operations=[
            {"op": "set_field", "field": "title", "target": "https://reyes.example/about/", "value": long_title, "previous_value": "About"},
            {"op": "set_field", "field": "meta_description", "target": "https://reyes.example/about/", "value": "<b>Austin</b> plumbers since 1998.", "previous_value": ""},
            {"op": "publish_page", "target": None, "value": {"title": "Drain cleaning in Austin", "body_markdown": "..."}},
        ]
    )
    title, description, publish = receipts.change_details(change_set)
    assert title["what"] == "Changed the title Google shows for this page" and title["before"] == "About" and len(title["after"]) <= 161 and title["after"].endswith("…")
    assert description["what"] == "Added a description Google can show for this page" and description["after"] == "Austin plumbers since 1998." and description["visible_to_visitors"] is False
    assert publish["what"] == "Published a new page" and publish["after"] == "Drain cleaning in Austin" and publish["page_url"] is None
    for change in (title, description, publish):
        assert not any(term in json.dumps(change).lower() for term in JARGON_ON_SCREEN)


def test_headlines_use_the_card_title_in_past_tense_and_undo_reads_undid():
    proposal = _proposal(status="approved", display_title="Add a business card for search engines to your homepage", reviewed_at=NOW - timedelta(days=1))
    applied = _change_set(status="reverted", reverted_by="r1")
    undo = _change_set(id="r1", status="applied", created_at=NOW)
    entry = receipts.ledger_entry(applied, proposal)
    assert entry["headline"] == "Added a business card for search engines to your homepage"
    assert (entry["approved_by"], entry["is_undo"], entry["undone_by_change_set_id"], entry["page_label"]) == ("you", False, "r1", "Homepage")
    undo_entry = receipts.ledger_entry(undo, proposal, original=applied)
    assert undo_entry["headline"] == "Undid: added a business card for search engines to your homepage"
    assert (undo_entry["approved_by"], undo_entry["is_undo"], undo_entry["page_url"]) == ("you", True, "https://reyes.example/")
    auto = receipts.ledger_entry(_change_set(), _proposal(status="auto_approved"))
    assert auto["approved_by"] == "automatic" and auto["headline"] == "Added a business card for search engines to your homepage"  # derived when the card has no title
    pending = receipts.ledger_entry(_change_set(status="pending"), proposal)
    assert pending["headline"] == "Add a business card for search engines to your homepage"


def test_apply_notification_names_the_change_and_place():
    title, body = receipts.change_notification_text(_change_set(), _proposal(display_title="Add a business card for search engines to your homepage"))
    assert title == "Added a business card for search engines to your homepage"
    assert body == "Live now. Visitors won't see a difference. Tap to see it."


##############################
# DONE FOR YOU
##############################


def test_done_for_you_is_outcomes_only_newest_first():
    since = NOW - timedelta(days=30)
    live = _change_set(id="live", applied_at=NOW - timedelta(days=2))
    undone = _change_set(id="undone", status="reverted", reverted_by="undo", applied_at=NOW - timedelta(days=5))
    undo = _change_set(id="undo", applied_at=NOW - timedelta(days=1), created_at=NOW - timedelta(days=1))
    old = _change_set(id="old", applied_at=NOW - timedelta(days=60))
    items = receipts.change_outcomes([live, undone, undo, old], {"a1": _proposal(display_title="Add a business card for search engines to your homepage")}, since)
    artifacts = [
        SimpleNamespace(id="p", type="plan", status="pending_review", content={}, evidence={}, created_at=NOW - timedelta(days=3)),
        SimpleNamespace(id="c", type="change_proposal", status="pending_review", content={}, evidence={}, created_at=NOW),
        SimpleNamespace(id="fr", type="flag_request", status="pending_review", content={}, evidence={}, created_at=NOW),
        SimpleNamespace(id="rj", type="draft", status="rejected", content={"title": "x"}, evidence={}, created_at=NOW),
        SimpleNamespace(id="d", type="draft", status="pending_review", content={"kind": "new", "title": "Drain cleaning in Austin"}, evidence={}, created_at=NOW - timedelta(days=4)),
        SimpleNamespace(id="r", type="reply", status="pending_review", content={}, evidence={"rating": 2}, created_at=NOW - timedelta(days=6)),
    ]
    items += [receipts.artifact_outcome(artifact) for artifact in artifacts]
    audit = SimpleNamespace(id="t-audit", name="site_audit", created_at=NOW - timedelta(days=7, hours=1), finished_at=NOW - timedelta(days=7))
    research = SimpleNamespace(id="t-research", name="research_prompts", created_at=NOW - timedelta(days=8, hours=1), finished_at=NOW - timedelta(days=8))
    items += [receipts.audit_outcome(audit, 6, is_first=True), receipts.research_outcome(research, 15)]

    feed = receipts.done_for_you(items, since)
    assert [(item["id"], item["kind"]) for item in feed] == [("undone", "fix_undone"), ("live", "fix_live"), ("p", "plan"), ("d", "draft"), ("r", "reply"), ("t-audit", "audit"), ("t-research", "research")]
    by_id = {item["id"]: item for item in feed}
    assert by_id["undone"]["title"] == "Undid: added a business card for search engines to your homepage" and by_id["undone"]["ref"] == {"type": "change_set", "id": "undone"}
    assert by_id["live"]["title"] == "Added a business card for search engines to your homepage"
    assert by_id["t-audit"]["title"] == "Checked your website for problems: found 6"
    assert by_id["t-research"]["title"] == "Found 15 questions your customers ask"
    assert by_id["d"]["title"] == "Wrote a new page draft: Drain cleaning in Austin" and by_id["d"]["ref"] == {"type": "artifact", "id": "d"}
    assert by_id["r"]["title"] == "Wrote a reply to a 2-star review"
    assert feed[0]["when"].endswith("+00:00")
    assert len(receipts.done_for_you(items, since, limit=3)) == 3


def test_audit_and_research_titles_never_overclaim():
    task = SimpleNamespace(id="t", name="site_audit", finished_at=NOW)
    assert receipts.audit_outcome(task, 0, is_first=False)["title"] == "Checked your website for problems: nothing new"
    assert receipts.audit_outcome(task, 2, is_first=False)["title"] == "Checked your website for problems: found 2 new"
    assert receipts.research_outcome(SimpleNamespace(id="t", name="research_competitors", finished_at=NOW), 0)["title"] == "Checked your competitors: no new ones"
    assert receipts.research_outcome(SimpleNamespace(id="t", name="metrics_ingestion", finished_at=NOW), 3) is None
    assert receipts.count_in_window([NOW, NOW - timedelta(hours=2), None], NOW - timedelta(hours=1), NOW) == 1


##############################
# PLANS + ASK
##############################


def test_plan_workstreams_become_bullets():
    content = {"summary_markdown": "prose", "workstreams": [{"name": "Get named for drain cleaning", "why": "w", "expected_impact": "Show up when people ask for drain cleaning", "first_moves": ["Write the page"], "needs_client_data": False}, {"name": "Case study", "why": "w2"}, {"why": "nameless"}]}
    rows = receipts.plan_workstreams(content, needs_you_names={"Case study"})
    assert rows == [
        {"title": "Get named for drain cleaning", "outcome": "Show up when people ask for drain cleaning", "first_steps": ["Write the page"], "needs_you": False},
        {"title": "Case study", "outcome": "w2", "first_steps": [], "needs_you": True},
    ]
    assert receipts.plan_workstreams(None) == []


def test_answer_summary_is_the_first_line_without_markdown():
    assert receipts.answer_summary("**Yes: we fixed 3 things on your site.**\n- Added a business card") == "Yes: we fixed 3 things on your site."
    assert receipts.answer_summary("\n\n## Short answer\nmore") == "Short answer"
    assert receipts.answer_summary("") == ""


##############################
# READ ENDPOINTS (glue over the derivations, objects faked)
##############################


async def test_worklog_and_change_detail_carry_the_contract_keys(monkeypatch):
    import api.reads as reads

    now = reads.clock.now()
    finding = _finding(created_at=now - timedelta(days=2))
    proposal = _proposal(status="approved", display_title="Add a business card for search engines to your homepage", created_at=now - timedelta(days=2), reviewed_at=now - timedelta(days=1))
    change_set = _change_set(applied_at=now - timedelta(days=1), created_at=now - timedelta(days=1))
    audit = SimpleNamespace(id="t-audit", name="site_audit", status="done", created_at=now - timedelta(days=2, minutes=5), finished_at=now - timedelta(days=2) + timedelta(minutes=1))

    def returns(value):
        async def fake(*args, **kwargs):
            return value

        return fake

    monkeypatch.setattr(reads.Brand, "get", returns(SimpleNamespace(id="b1")))
    monkeypatch.setattr(reads.Task, "for_brand", returns([]))
    monkeypatch.setattr(reads.Task, "done_since", lambda brand_id, names, since=None: returns([audit] if "site_audit" in names else [])())
    monkeypatch.setattr(reads.Finding, "open_for_brand", returns([]))
    monkeypatch.setattr(reads.Finding, "resolved_since", returns([SimpleNamespace(**{**finding.__dict__, "status": "fixed", "resolved_at": now - timedelta(days=1)})]))
    monkeypatch.setattr(reads.Finding, "created_since", returns([finding]))
    monkeypatch.setattr(reads.ChangeSet, "for_brand", returns([change_set]))
    monkeypatch.setattr(reads.ChangeSet, "get", returns(change_set))
    monkeypatch.setattr(reads.ChangeSet, "reverted_from", returns(None))
    monkeypatch.setattr(reads.Artifact, "by_ids", returns([proposal]))
    monkeypatch.setattr(reads.Artifact, "for_brand", returns([proposal]))
    monkeypatch.setattr(reads.Artifact, "created_since", returns([proposal]))
    monkeypatch.setattr(reads.Report, "for_brand", returns([]))

    worklog = await reads.worklog("b1")
    assert {"site_health", "running", "queued", "shipped", "change_sets", "issues", "done_for_you"} <= worklog.keys()
    (issue,) = worklog["issues"]
    assert (issue["status"], issue["dot"], issue["change_set_id"], issue["artifact_id"]) == ("fix_live", "green", "cs1", "a1")
    assert [item["kind"] for item in worklog["done_for_you"]] == ["fix_live", "audit"]  # the proposal itself is not an outcome
    assert worklog["done_for_you"][1]["title"] == "Checked your website for problems: found 1"
    (entry,) = worklog["change_sets"]
    assert {"id", "status", "operation_count", "operations", "applied_at", "reverted_by"} <= entry.keys()
    assert (entry["headline"], entry["approved_by"], entry["page_label"], entry["is_undo"]) == ("Added a business card for search engines to your homepage", "you", "Homepage", False)

    detail = await reads.change_set_detail("cs1")
    assert detail["headline"] == entry["headline"] and detail["changes"][0]["what"] == "Added a business card for search engines"
    assert "<script" not in json.dumps(detail["changes"])


class _Capture:
    def __init__(self):
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [], first=lambda: None))


def _compiled(statement) -> str:
    from sqlalchemy.dialects import postgresql

    return " ".join(str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})).split())


async def test_new_queries_read_what_they_say(monkeypatch):
    from objects.artifact import Artifact
    from objects.change_set import ChangeSet
    from objects.finding import Finding
    from objects.task import Task
    from utils.database import Database

    capture = _Capture()
    monkeypatch.setattr(Database, "get_session", lambda self: capture)
    await Finding.fixed_by_change_for_brand("b1")
    await ChangeSet.for_artifact("a1")
    await ChangeSet.reverted_from("cs1")
    await Task.done_since("b1", ["site_audit"], since=NOW)
    await Artifact.created_since("b1", NOW)
    fixed, chain, reverted_from, done, created = (_compiled(statement) for statement in capture.statements)
    assert "findings.status = 'fixed'" in fixed and "findings.detail ? 'resolved_by_change_set_id'" in fixed
    assert "change_sets.artifact_id = 'a1'" in chain
    assert "change_sets.reverted_by_change_set_id = 'cs1'" in reverted_from
    assert "tasks.status = 'done'" in done and "tasks.name IN ('site_audit')" in done and "tasks.finished_at >=" in done
    assert "artifacts.created_at >=" in created and "ORDER BY artifacts.created_at DESC" in created


def test_ask_prompt_asks_for_a_one_line_answer_then_bullets():
    import api.ask as ask

    prompt = ask._ask_prompt(SimpleNamespace(name="Reyes", website_url="https://reyes.example"), "what have you done for me?", {})
    assert "at most 15 words" in prompt and "at most 5 short bullets" in prompt and "no tables unless the owner asks" in prompt.lower()
    assert "Never promise a diagnosis" in prompt  # the honesty rule stays


def test_page_label_reads_as_words():
    assert receipts.page_label("https://reyes.example/services/drain-cleaning/") == "Drain cleaning page"
    assert receipts.page_label("https://reyes.example/contact.html") == "Contact page"
    assert receipts.page_label("https://reyes.example") == "Homepage"
    assert receipts.page_phrase("https://reyes.example/about-us/") == "your about us page"


def test_plan_outcome_is_one_sentence():
    rows = receipts.plan_workstreams({"workstreams": [{"name": "Fix the site", "expected_impact": "Six problems closed. Then more detail nobody reads."}]})
    assert rows[0]["outcome"] == "Six problems closed."


def test_research_and_audit_rows_open_their_screen():
    assert receipts.audit_outcome(SimpleNamespace(id="a", finished_at=NOW), 6, is_first=True)["ref"] == {"type": "screen", "id": "audit"}
    questions = receipts.research_outcome(SimpleNamespace(id="q", name="research_prompts", finished_at=NOW), 15)
    assert questions["ref"] == {"type": "screen", "id": "questions"}
    competitors = receipts.research_outcome(SimpleNamespace(id="c", name="research_competitors", finished_at=NOW), 12)
    assert competitors["title"] == "Found the businesses AI assistants name alongside you" and "12" not in competitors["title"]


def test_change_notices_batch_into_one():
    title, body, link = receipts.batched_change_notice("Added a business card for search engines to your homepage", "Live now.", "change_sets/a", "Added a business card for search engines to your contact page", "b")
    assert title == "We made 2 changes to your site" and link == "changes/a,b"
    assert body.splitlines() == ["- Added a business card for search engines to your homepage", "- Added a business card for search engines to your contact page"]
    title, body, link = receipts.batched_change_notice(title, body, link, "Undid the change to your about page", "c")
    assert title == "We made 3 changes to your site" and link == "changes/a,b,c" and body.splitlines()[-1] == "- Undid the change to your about page"


def test_business_card_change_links_to_a_checker():
    entry = receipts.describe_operation({"op": "set_field", "field": "content", "target": "https://reyes.example/", "previous_value": "<p>Hi</p>", "value": '<p>Hi</p><script type="application/ld+json">{"@type":"Organization","name":"Reyes"}</script>'})
    assert entry["check_url"] == "https://validator.schema.org/#url=https%3A%2F%2Freyes.example%2F"
