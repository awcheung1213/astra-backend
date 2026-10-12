"""BUG-049 — agent-mode technical_fix dedups BEFORE the paid run: findings
with a live proposal or an earlier skip never reach the agent, and a run
with nothing left never calls it. Pure (monkeypatched, no DB)."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import settings
from worker.tasks import fixes
from worker.tasks.fixes import MODE_AGENT, findings_needing_agent


def _finding(finding_id: str, detail: dict | None = None):
    return SimpleNamespace(id=finding_id, kind="missing_schema", summary="s", detail=detail or {"url": "https://x.com/"})


def test_findings_needing_agent_drops_covered_and_skipped():
    findings = [_finding("a"), _finding("b"), _finding("c", {"url": "https://x.com/", "skipped": {"reason": "host setting"}}), _finding("d")]
    assert [finding.id for finding in findings_needing_agent(findings, {"b"})] == ["a", "d"]
    assert findings_needing_agent(findings[1:3], {"b"}) == []


def _wire(monkeypatch, findings: list, covered: set[str]) -> list:
    agent_calls: list = []

    @asynccontextmanager
    async def lifecycle(task_id):
        yield SimpleNamespace(payload={"finding_ids": [finding.id for finding in findings]})

    async def open_finding(finding_id):
        return next(finding for finding in findings if finding.id == finding_id)

    async def get_brand(brand_id):
        return SimpleNamespace(id=brand_id)

    async def existing(brand_id, finding_id):
        return SimpleNamespace(id=f"art-{finding_id}") if finding_id in covered else None

    async def run_agent(task_id, brand, run_findings):
        agent_calls.append([finding.id for finding in run_findings])

    monkeypatch.setattr(settings, "TECHNICAL_FIX_MODE", MODE_AGENT)
    monkeypatch.setattr(fixes, "task_lifecycle", lifecycle)
    monkeypatch.setattr(fixes, "_open_finding", open_finding)
    monkeypatch.setattr(fixes.Brand, "get", get_brand)
    monkeypatch.setattr(fixes, "_existing_proposal", existing)
    monkeypatch.setattr(fixes, "_run_agent", run_agent)
    return agent_calls


async def test_agent_gets_only_uncovered_findings(monkeypatch):
    findings = [_finding("schema1"), _finding("schema2"), _finding("meta1"), _finding("meta2", {"url": "https://x.com/", "skipped": {"reason": "r"}})]
    calls = _wire(monkeypatch, findings, covered={"schema1", "schema2"})
    await fixes.technical_fix.func("t1", "b1")
    assert calls == [["meta1"]]


async def test_all_covered_never_calls_the_agent(monkeypatch):
    findings = [_finding("schema1"), _finding("schema2"), _finding("schema3")]
    calls = _wire(monkeypatch, findings, covered={"schema1", "schema2", "schema3"})
    await fixes.technical_fix.func("t1", "b1")
    assert calls == []
