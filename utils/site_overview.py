"""The "Your site" screen's numbers, in owner words — pure shapers over rows
the callers fetch (tests/test_site_pages.py), plus the one async rollup the
worklog calls:

- site_pages: the TRUE page count from the sitemap, how many the weekly
  rotation has checked so far (healthy / need a fix / slow / broken), how long
  until every page has had its turn, and the same per section — what the
  section bars draw.
- issue_groups: "fix by problem" — open problems grouped by kind ("No Google
  description · 4 pages"), with the pending fix ids the one-click "Review 4"
  button approves through POST /brands/{id}/approvals/batch.

Copy follows utils/receipts.py (the Marco test): no "meta", "schema",
"sitemap" or "crawler" reaches the screen."""

import math
from datetime import datetime

from objects.site_page import STATUS_BROKEN, STATUS_HEALTHY, STATUS_NEEDS_FIX, STATUS_SLOW, STATUS_UNCHECKED, SitePage
from schemas.base_schemas import utc_iso
from utils import receipts
from worker.tasks.site_audit import KIND_BLOCKED_AI_CRAWLER, KIND_BROKEN_LINK, KIND_MISSING_H1, KIND_MISSING_META_DESCRIPTION, KIND_MISSING_SCHEMA, KIND_MISSING_SITEMAP, KIND_MISSING_TITLE, KIND_SLOW_PAGE, MAX_PAGES, finding_key

##############################
# SITE PAGES (counts + sections)
##############################


def summarize_site_pages(rows: list[tuple[str, str, int, datetime | None]], per_week: int = MAX_PAGES) -> dict:
    """(section, status, pages, newest last_checked_at) rows -> the worklog's
    site_pages block. checked = every status but unchecked; weeks_to_full =
    weekly audits until every page has been checked once. Pure."""
    by_section: dict[str, dict] = {}
    totals = {"total": 0, "checked": 0, STATUS_HEALTHY: 0, STATUS_NEEDS_FIX: 0, STATUS_SLOW: 0, STATUS_BROKEN: 0}
    last_checked_at: datetime | None = None
    for section, status, pages, section_last_checked_at in rows:
        entry = by_section.setdefault(section, {"section": section, "total": 0, "checked": 0, STATUS_HEALTHY: 0, STATUS_NEEDS_FIX: 0, STATUS_SLOW: 0, STATUS_BROKEN: 0})
        for bucket in (entry, totals):
            bucket["total"] += pages
            if status != STATUS_UNCHECKED:
                bucket["checked"] += pages
            if status in (STATUS_HEALTHY, STATUS_NEEDS_FIX, STATUS_SLOW, STATUS_BROKEN):
                bucket[status] += pages
        if section_last_checked_at and (last_checked_at is None or section_last_checked_at > last_checked_at):
            last_checked_at = section_last_checked_at
    not_checked = totals["total"] - totals["checked"]
    return {
        **totals,
        "per_week": per_week,
        "weeks_to_full": math.ceil(not_checked / per_week) if per_week > 0 else 0,
        "by_section": sorted(by_section.values(), key=lambda entry: (-entry["total"], entry["section"])),
        "last_checked_at": utc_iso(last_checked_at),
    }


async def site_pages_summary(brand_id: str) -> dict:
    """The worklog's `site_pages` key (current pages only — see SitePage.AGE_OUT_DAYS)."""
    return summarize_site_pages(await SitePage.status_counts(brand_id))


def page_entry(page) -> dict:
    """One page-search row: the page in owner words ('About page'), its
    section and status."""
    return {"url": page.url, "label": receipts.page_label(page.url), "section": page.section, "status": page.status, "last_checked_at": utc_iso(page.last_checked_at)}


##############################
# ISSUE GROUPS ("fix by problem")
##############################

# one line per problem KIND (receipts.issue_title's per-page wording, made plural-safe)
ISSUE_GROUP_TITLES = {
    KIND_MISSING_META_DESCRIPTION: "No Google description",
    KIND_MISSING_TITLE: "No Google title",
    KIND_MISSING_H1: "No main heading",
    KIND_MISSING_SCHEMA: "No business card for search engines",
    KIND_SLOW_PAGE: "Slow to load",
    KIND_BROKEN_LINK: "Broken pages",
    KIND_BLOCKED_AI_CRAWLER: "AI assistants are blocked from reading your site",
    KIND_MISSING_SITEMAP: "No page list for search engines",
}
GROUP_STATUS_ORDER = {"fix_ready": 0, "needs_fix": 1, "cant_fix": 2, "fixed": 3}
OPEN_ISSUE_STATUSES = set(receipts.OPEN_ORDER)  # fix_waiting | open | cant_fix


def issue_group_title(kind: str, fallback: str) -> str:
    return ISSUE_GROUP_TITLES.get(kind, fallback)


def issue_groups(findings: list, proposals: list, change_sets: list) -> list[dict]:
    """Findings (open + recently resolved) grouped by kind. Each finding's
    status comes from receipts.issue_entry (the same lifecycle the issues list
    shows), so a group is:
      fix_ready — at least one fix waiting for the owner's OK (artifact_ids =
                  those pending fixes; the "Review N" button approves them all)
      needs_fix — open, nothing ready to approve yet
      cant_fix  — open, but nothing we can fix automatically
      fixed     — nothing open any more (recently resolved only)
    pages = distinct pages with the problem open (or, once fixed, that had it).
    Fix-ready first, then by pages. Pure."""
    proposals_by_finding: dict[str, list] = {}
    for proposal in proposals:
        for finding_id in receipts.proposal_finding_ids(proposal.content):
            proposals_by_finding.setdefault(finding_id, []).append(proposal)
    change_sets_by_artifact: dict[str, list] = {}
    for change_set in change_sets:
        change_sets_by_artifact.setdefault(change_set.artifact_id, []).append(change_set)

    grouped: dict[str, list[tuple]] = {}
    for finding in findings:
        entry = receipts.issue_entry(finding, proposals_by_finding.get(finding.id, []), change_sets_by_artifact)
        grouped.setdefault(finding.kind, []).append((finding, entry))

    groups: list[dict] = []
    for kind, members in grouped.items():
        open_members = [(finding, entry) for finding, entry in members if entry["status"] in OPEN_ISSUE_STATUSES]
        counted = open_members or members
        artifact_ids: list[str] = []
        for _, entry in open_members:
            if entry["status"] == "fix_waiting" and entry["artifact_id"] and entry["artifact_id"] not in artifact_ids:
                artifact_ids.append(entry["artifact_id"])
        open_statuses = {entry["status"] for _, entry in open_members}
        if artifact_ids:
            status = "fix_ready"
        elif "open" in open_statuses or "fix_waiting" in open_statuses:
            status = "needs_fix"
        elif "cant_fix" in open_statuses:
            status = "cant_fix"
        else:
            status = "fixed"
        groups.append(
            {
                "kind": kind,
                "title": issue_group_title(kind, members[0][1]["title"]),
                "pages": len({finding_key(finding.kind, finding.detail or {}) for finding, _ in counted}),
                "fixes_ready": len(artifact_ids),
                "artifact_ids": artifact_ids,
                "status": status,
            }
        )
    groups.sort(key=lambda group: (GROUP_STATUS_ORDER[group["status"]], -group["pages"], group["title"]))
    return groups
