"""Competitor review — the wizard's "competitors we found" step and the
dashboard's competitor management share these three routes: list what the
bootstrap + run-time discovery hold, add one the client knows about, and
exclude one that's wrong (excluded = never matched, never resurrected —
removal is exclusion, not deletion, so discovery can't re-add it).

Auth rides the router-level guard (utils/authz.py): bearer token + the
brand-in-path must belong to the caller's org."""

from fastapi import APIRouter, HTTPException, status

from objects.brand import Brand
from objects.competitor import SOURCE_USER, Competitor
from schemas.competitor_schemas import CompetitorAddRequest, CompetitorItem, CompetitorListResponse, CompetitorRemoveResponse

router = APIRouter(prefix="/api/v1")


@router.get("/brands/{brand_id}/competitors", response_model=CompetitorListResponse)
async def list_competitors(brand_id: str) -> CompetitorListResponse:
    brand = await Brand.get(brand_id)
    rows = await Competitor.get_competitors_by_brand_id(brand.id)
    rows.sort(key=lambda competitor: competitor.created_at or competitor.name)
    return CompetitorListResponse.create({"brand_id": brand.id, "competitors": [CompetitorItem.create({"id": row.id, "name": row.name, "is_active": row.is_active}) for row in rows]})


@router.post("/brands/{brand_id}/competitors", response_model=CompetitorItem)
async def add_competitor(brand_id: str, request: CompetitorAddRequest) -> CompetitorItem:
    brand = await Brand.get(brand_id)
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="name is required")
    if Competitor.is_own_brand(brand.name, name):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="that's your own brand")
    # a client add un-excludes: the human overrides any earlier exclusion
    competitor = await Competitor.upsert(brand.id, name, source=SOURCE_USER, is_excluded=False)
    return CompetitorItem.create({"id": competitor.id, "name": competitor.name, "is_active": competitor.is_active})


@router.delete("/brands/{brand_id}/competitors/{competitor_id}", response_model=CompetitorRemoveResponse)
async def remove_competitor(brand_id: str, competitor_id: str) -> CompetitorRemoveResponse:
    await Brand.get(brand_id)
    competitor = await Competitor.update(competitor_id, brand_id=brand_id, is_excluded=True)
    return CompetitorRemoveResponse.create({"id": competitor.id, "is_excluded": competitor.is_excluded})
