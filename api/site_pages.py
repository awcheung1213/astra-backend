"""The "Your site" screen's two actions: page search over the site's true page
list (site_pages, kept by the audit) and the "fix by problem" one-click
approve — "Review 4" approves every ready fix of one problem at once.

The batch approve is NOT a second approval path: each id goes through the
review endpoint itself (api/approvals.review_artifact, decision approve), so
the pending-only 409, the site-not-connected 409, the row lock and the
per-type fan-out are exactly a single click's. Two extra per-item refusals,
checked first: an id that isn't this brand's artifact (the guard only proves
the PATH brand is the caller's), and the always-approve floor types
(utils/approval_flow.ALWAYS_APPROVE_ARTIFACT_TYPES — a reply, pitch or GBP post
goes out under the owner's name and is never approved in bulk). Each item runs
in its own SAVEPOINT, so one refusal never undoes the others; the request's
one transaction commits whatever succeeded. Auth rides the router-level guard
(brand_id in the path)."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field

from api.approvals import review_artifact
from objects.artifact import Artifact
from objects.brand import Brand
from objects.site_page import SitePage
from schemas.approval_schemas import DECISION_APPROVE, ReviewRequest
from schemas.base_schemas import BaseRequest
from utils.approval_flow import requires_human_approval
from utils.database import Database
from utils.logger import logger
from utils.site_overview import page_entry

router = APIRouter(prefix="/api/v1")

MAX_BATCH_APPROVALS = 50
SEARCH_LIMIT_MAX = 100

DETAIL_NOT_FOUND = "We couldn't find this one."
DETAIL_REVIEW_ON_ITS_OWN = "This one goes out under your name, so it needs your OK on its own."
DETAIL_FAILED = "Something went wrong approving this one. Try it on its own."


class BatchApprovalRequest(BaseRequest):
    artifact_ids: list[str] = Field(min_length=1, max_length=MAX_BATCH_APPROVALS)


def batch_refusal(artifact: Artifact | None, brand_id: str) -> str | None:
    """Why an id is refused before the review path runs, or None: not found
    / another brand's (one answer for both — no cross-tenant probing), or a
    floor type that only ever gets a single, deliberate click. Pure."""
    if artifact is None or artifact.brand_id != brand_id:
        return DETAIL_NOT_FOUND
    if requires_human_approval(artifact.type):
        return DETAIL_REVIEW_ON_ITS_OWN
    return None


def batch_result(artifact_id: str, ok: bool, detail: str | None = None) -> dict:
    return {"artifact_id": artifact_id, "ok": ok, "detail": None if ok else detail}


def unique_ids(artifact_ids: list[str]) -> list[str]:
    """Order kept, repeats and blanks dropped (a double-click sends one id twice). Pure."""
    seen: list[str] = []
    for artifact_id in artifact_ids:
        if artifact_id and artifact_id not in seen:
            seen.append(artifact_id)
    return seen


@router.post("/brands/{brand_id}/approvals/batch")
async def approve_batch(brand_id: str, request: BatchApprovalRequest, http_request: Request) -> dict:
    """Approve up to 50 pending artifacts of this brand, each through the
    single-review path. Always 200 with a result per id (in request order):
    {"results": [{"artifact_id", "ok", "detail"}]} — detail is the plain
    reason an item was refused (the review endpoint's own 409 text included)."""
    await Brand.get(brand_id)
    artifact_ids = unique_ids(request.artifact_ids)
    artifacts_by_id = {artifact.id: artifact for artifact in await Artifact.by_ids(artifact_ids)}

    results = []
    for artifact_id in artifact_ids:
        refusal = batch_refusal(artifacts_by_id.get(artifact_id), brand_id)
        if refusal:
            results.append(batch_result(artifact_id, False, refusal))
            continue
        try:
            async with Database.session.begin_nested():  # type: ignore[attr-defined]  # this item only: a refusal rolls back to here
                await review_artifact(artifact_id, ReviewRequest(decision=DECISION_APPROVE), http_request)
        except HTTPException as exc:
            results.append(batch_result(artifact_id, False, str(exc.detail)))
            continue
        except Exception as exc:  # noqa: BLE001 — one bad item must not sink the rest; its savepoint already rolled back
            logger.error(f"[approvals] batch approve of {artifact_id} failed: {exc}")
            results.append(batch_result(artifact_id, False, DETAIL_FAILED))
            continue
        results.append(batch_result(artifact_id, True))
    return {"results": results}


@router.get("/brands/{brand_id}/site_pages")
async def site_pages(brand_id: str, q: str | None = None, section: str | None = None, limit: int = 20) -> dict:
    """The page search box: current pages whose address contains q
    (case-insensitive), optionally one section, problems first.
    {"pages": [{"url", "label", "section", "status", "last_checked_at"}]}."""
    await Brand.get(brand_id)
    pages = await SitePage.search(brand_id, q=(q or "").strip() or None, section=section or None, limit=max(1, min(limit, SEARCH_LIMIT_MAX)))
    return {"pages": [page_entry(page) for page in pages]}
