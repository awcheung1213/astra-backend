"""Market typeahead — the coverage step's city autofill. Read-only, shaped
like the read layer (plain dict). NO AUTH YET (repo-wide)."""

from fastapi import APIRouter

from objects.market import Market

router = APIRouter(prefix="/api/v1")


@router.get("/markets")
async def search_markets(q: str = "", limit: int = 10) -> dict:
    return {"markets": [{"city": market.city, "state": market.state} for market in await Market.search(q, limit=min(limit, 25))]}
