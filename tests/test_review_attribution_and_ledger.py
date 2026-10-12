"""Pure (no DB, no client): an approval records the VERIFIED caller as reviewed_by
(BUG-027), and the Changes ledger describes each op without shipping its value (BUG-024)."""

from types import SimpleNamespace

import api.approvals as approvals
from api.reads import _ledger_operation
from schemas.approval_schemas import ReviewRequest


async def test_review_records_the_verified_caller_not_a_client_sent_reviewer(monkeypatch):
    recorded = {}
    pending = SimpleNamespace(id="art-1", brand_id="b-1", type="plan", status="pending_review")

    async def get_for_update(artifact_id):
        return pending

    async def no_site_needed(brand_id, artifact_type):
        return False

    async def review(artifact_id, status, reviewed_by=None):
        recorded["reviewed_by"] = reviewed_by
        return SimpleNamespace(id=artifact_id, status=status)

    async def fan_out(artifact, approved_rows):
        return []

    monkeypatch.setattr(approvals.Artifact, "get_for_update", staticmethod(get_for_update))
    monkeypatch.setattr(approvals.Artifact, "review", staticmethod(review))
    monkeypatch.setattr(approvals, "needs_site_connection", no_site_needed)
    monkeypatch.setattr(approvals, "fan_out_approval", fan_out)

    http_request = SimpleNamespace(state=SimpleNamespace(current_user=SimpleNamespace(id="user-verified")))
    response = await approvals.review_artifact("art-1", ReviewRequest(decision="approve", reviewed_by="spoofed"), http_request)

    assert recorded["reviewed_by"] == "user-verified"
    assert response.status == "approved"


def test_ledger_operation_names_the_field_page_and_new_page_title_but_never_the_value():
    field_write = {"id": "op-0", "op": "set_field", "field": "meta_description", "target": "https://x.com/about/", "value": "a long description", "previous_value": "old"}
    assert _ledger_operation(field_write) == {"op": "set_field", "field": "meta_description", "target": "https://x.com/about/", "title": None}

    publish = {"id": "op-0", "op": "publish_page", "target": None, "value": {"title": "AC repair in Mesa", "body_markdown": "..."}}
    assert _ledger_operation(publish) == {"op": "publish_page", "field": None, "target": None, "title": "AC repair in Mesa"}
