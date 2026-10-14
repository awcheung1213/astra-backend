"""The national city panel — Lean collection (owner decision 2026-10-14).

A national brand no longer collects in all ~150 seeded system markets: it
collects in ONE shared, fixed panel of settings.NATIONAL_PANEL_SIZE (20) system
cities + the nationwide pseudo-market. Every seeded market row stays (history
intact, re-ask candidates); non-panel system rows are just is_active=false.

Panel rule: the largest city (by population) in each of the 9 US Census
divisions, ranked by population (ranks 1-9), then the next largest system
cities nationwide (ranks 10-20). The small panel (long-tail questions) is the
top settings.SMALL_PANEL_SIZE ranks — five different divisions.

Everything here is PURE (rows in, ids/ranks out — tests/test_city_panel.py);
objects/market.py does the reads/writes. The migration that first applied the
panel carries its own frozen copy of select_panel (migrations never import app
code); a test pins the two together."""

from typing import Any, Iterable, NamedTuple

NEW_ENGLAND = "New England"
MIDDLE_ATLANTIC = "Middle Atlantic"
EAST_NORTH_CENTRAL = "East North Central"
WEST_NORTH_CENTRAL = "West North Central"
SOUTH_ATLANTIC = "South Atlantic"
EAST_SOUTH_CENTRAL = "East South Central"
WEST_SOUTH_CENTRAL = "West South Central"
MOUNTAIN = "Mountain"
PACIFIC = "Pacific"
DIVISIONS = (NEW_ENGLAND, MIDDLE_ATLANTIC, EAST_NORTH_CENTRAL, WEST_NORTH_CENTRAL, SOUTH_ATLANTIC, EAST_SOUTH_CENTRAL, WEST_SOUTH_CENTRAL, MOUNTAIN, PACIFIC)

# USPS state (50 + DC) -> US Census Bureau division
DIVISION_BY_STATE = {
    **{state: NEW_ENGLAND for state in ("CT", "ME", "MA", "NH", "RI", "VT")},
    **{state: MIDDLE_ATLANTIC for state in ("NJ", "NY", "PA")},
    **{state: EAST_NORTH_CENTRAL for state in ("IL", "IN", "MI", "OH", "WI")},
    **{state: WEST_NORTH_CENTRAL for state in ("IA", "KS", "MN", "MO", "NE", "ND", "SD")},
    **{state: SOUTH_ATLANTIC for state in ("DE", "DC", "FL", "GA", "MD", "NC", "SC", "VA", "WV")},
    **{state: EAST_SOUTH_CENTRAL for state in ("AL", "KY", "MS", "TN")},
    **{state: WEST_SOUTH_CENTRAL for state in ("AR", "LA", "OK", "TX")},
    **{state: MOUNTAIN for state in ("AZ", "CO", "ID", "MT", "NV", "NM", "UT", "WY")},
    **{state: PACIFIC for state in ("AK", "CA", "HI", "OR", "WA")},
}


class PanelCandidate(NamedTuple):
    id: str
    city: str
    state: str
    population: int | None


def division_of(state: str | None) -> str | None:
    return DIVISION_BY_STATE.get((state or "").strip().upper())


def candidate(row: Any) -> PanelCandidate:
    """A markets row (or any object with id/city/state/population) as a candidate."""
    return PanelCandidate(id=row.id, city=row.city, state=row.state, population=row.population)


def _largest_first(candidate: PanelCandidate) -> tuple:
    # population desc; ties stable on city, then id (deterministic re-runs)
    return (-(candidate.population or 0), (candidate.city or "").lower(), candidate.id)


def select_panel(candidates: Iterable[PanelCandidate], size: int) -> dict[str, int]:
    """{market id: panel rank 1..size}. Division winners first (largest city
    per division, ranked by population), then the next largest nationwide.
    size < 9 keeps only the largest division winners."""
    ranked = sorted(candidates, key=_largest_first)
    winners: list[PanelCandidate] = []
    covered: set[str] = set()
    for row in ranked:
        division = division_of(row.state)
        if division and division not in covered:
            covered.add(division)
            winners.append(row)
    chosen = winners[:size]
    chosen_ids = {row.id for row in chosen}
    for row in ranked:
        if len(chosen) >= size:
            break
        if row.id not in chosen_ids:
            chosen.append(row)
            chosen_ids.add(row.id)
    return {row.id: rank for rank, row in enumerate(chosen, start=1)}


def promotion_plan(panel: Iterable[tuple[PanelCandidate, int]], pool: Iterable[PanelCandidate], size: int) -> dict[str, int]:
    """Refill vacated ranks after a panel city was deactivated: {pool id: rank}.

    panel = the still-active members with their ranks; pool = promotable
    system markets (inactive, never ranked, not excluded). Each vacated rank,
    lowest first, goes to the largest pool city in a division the panel no
    longer covers (keeps the 9-division spread), else the largest pool city."""
    members = list(panel)
    taken_ranks = {rank for _, rank in members}
    covered = {division_of(row.state) for row, _ in members}
    remaining = sorted(pool, key=_largest_first)
    promoted: dict[str, int] = {}
    for rank in range(1, size + 1):
        if rank in taken_ranks or not remaining:
            continue
        uncovered = [row for row in remaining if division_of(row.state) and division_of(row.state) not in covered]
        pick = uncovered[0] if uncovered else remaining[0]
        promoted[pick.id] = rank
        covered.add(division_of(pick.state))
        remaining.remove(pick)
    return promoted


def small_panel(markets: list[Any], footprint: str, size: int) -> list[Any]:
    """The long-tail market set from a brand's full (core) market list.
    National: the nationwide pseudo-market + the top `size` panel cities.
    Local: unchanged — its home cities."""
    from objects.market import SOURCE_NATIONAL  # lazy: keeps this module import-light

    if footprint != "national":
        return list(markets)
    nationwide = [market for market in markets if market.source == SOURCE_NATIONAL]
    cities = sorted((market for market in markets if market.source != SOURCE_NATIONAL), key=lambda market: (market.panel_rank is None, market.panel_rank or 0, -(market.population or 0)))
    return nationwide + cities[:size]


def reask_cities(markets: Iterable[Any], count: int) -> list[Any]:
    """The NEXT `count` ranked non-panel system cities for a borderline re-ask:
    shared system collector rows without a panel rank, largest first. Inactive
    rows qualify (non-panel system markets are inactive by design)."""
    from objects.market import SOURCE_SYSTEM

    pool = [market for market in markets if market.source == SOURCE_SYSTEM and market.brand_id is None and market.collection_market_id is None and market.panel_rank is None]
    return sorted(pool, key=lambda market: _largest_first(candidate(market)))[:count]
