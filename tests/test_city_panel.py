"""Lean collection — the national city panel (utils/city_panel.py) and the
migration's data step (alembic 86bb56a51abe). Pure: rows in, ranks out; no DB."""

import importlib.util
import pathlib
from types import SimpleNamespace

from constants.constants import STATE_ABBREV_BY_FIPS
from utils import city_panel
from utils.city_panel import DIVISION_BY_STATE, DIVISIONS, PanelCandidate, promotion_plan, reask_cities, select_panel, small_panel

MIGRATION_PATH = next(pathlib.Path(__file__).resolve().parent.parent.joinpath("alembic", "versions").glob("*_86bb56a51abe_markets_panel_rank.py"))


def _migration():
    spec = importlib.util.spec_from_file_location("panel_migration", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# (city, state, population): every division represented, several big same-division cities
CITIES = [
    ("New York", "NY", 8_300_000),
    ("Los Angeles", "CA", 3_900_000),
    ("Chicago", "IL", 2_700_000),
    ("Houston", "TX", 2_300_000),
    ("Phoenix", "AZ", 1_600_000),
    ("Philadelphia", "PA", 1_550_000),
    ("San Antonio", "TX", 1_450_000),
    ("San Diego", "CA", 1_380_000),
    ("Dallas", "TX", 1_300_000),
    ("Jacksonville", "FL", 950_000),
    ("Austin", "TX", 960_000),
    ("San Jose", "CA", 1_000_000),
    ("Fort Worth", "TX", 940_000),
    ("Columbus", "OH", 900_000),
    ("Charlotte", "NC", 880_000),
    ("Indianapolis", "IN", 870_000),
    ("San Francisco", "CA", 800_000),
    ("Seattle", "WA", 750_000),
    ("Denver", "CO", 715_000),
    ("Nashville", "TN", 690_000),
    ("Oklahoma City", "OK", 680_000),
    ("Boston", "MA", 650_000),
    ("Kansas City", "MO", 510_000),
    ("Omaha", "NE", 480_000),
    ("Providence", "RI", 190_000),
]


def _candidates() -> list[PanelCandidate]:
    return [PanelCandidate(id=f"m-{index}", city=city, state=state, population=population) for index, (city, state, population) in enumerate(CITIES)]


def _city(ranks: dict[str, int], rank: int) -> str:
    by_id = {candidate.id: candidate.city for candidate in _candidates()}
    return next(by_id[market_id] for market_id, market_rank in ranks.items() if market_rank == rank)


##############################
# DIVISION MAP
##############################


def test_division_map_covers_the_50_states_plus_dc_in_9_divisions():
    states = {abbrev for fips, abbrev in STATE_ABBREV_BY_FIPS.items() if int(fips) <= 56}
    assert set(DIVISION_BY_STATE) == states and len(states) == 51
    assert set(DIVISION_BY_STATE.values()) == set(DIVISIONS) and len(DIVISIONS) == 9
    assert DIVISION_BY_STATE["DC"] == city_panel.SOUTH_ATLANTIC and DIVISION_BY_STATE["TX"] == city_panel.WEST_SOUTH_CENTRAL
    assert DIVISION_BY_STATE["MO"] == city_panel.WEST_NORTH_CENTRAL and DIVISION_BY_STATE["KY"] == city_panel.EAST_SOUTH_CENTRAL
    assert city_panel.division_of(" ny ") == city_panel.MIDDLE_ATLANTIC and city_panel.division_of("PR") is None


##############################
# PANEL SELECTION
##############################


def test_panel_is_division_winners_by_population_then_the_next_largest():
    ranks = select_panel(_candidates(), 20)
    assert sorted(ranks.values()) == list(range(1, 21))
    winners = [_city(ranks, rank) for rank in range(1, 10)]
    assert winners == ["New York", "Los Angeles", "Chicago", "Houston", "Phoenix", "Jacksonville", "Nashville", "Boston", "Kansas City"]
    rest = [_city(ranks, rank) for rank in range(10, 21)]
    assert rest == ["Philadelphia", "San Antonio", "San Diego", "Dallas", "San Jose", "Austin", "Fort Worth", "Columbus", "Charlotte", "Indianapolis", "San Francisco"]
    assert "Providence" not in winners + rest and "Omaha" not in winners + rest


def test_panel_is_deterministic_and_handles_small_sizes_and_missing_divisions():
    assert select_panel(list(reversed(_candidates())), 20) == select_panel(_candidates(), 20)
    top5 = select_panel(_candidates(), 5)
    assert [_city(top5, rank) for rank in range(1, 6)] == ["New York", "Los Angeles", "Chicago", "Houston", "Phoenix"]
    texas_only = [candidate for candidate in _candidates() if candidate.state == "TX"]
    assert [candidate_id for candidate_id, _ in sorted(select_panel(texas_only, 3).items(), key=lambda item: item[1])] == ["m-3", "m-6", "m-8"]
    tie = [PanelCandidate("b", "Beta", "NY", None), PanelCandidate("a", "Alpha", "NY", None)]
    assert select_panel(tie, 2) == {"a": 1, "b": 2}


##############################
# MIGRATION DATA STEP
##############################


def _row(candidate: PanelCandidate, **overrides) -> dict:
    row = dict(id=candidate.id, city=candidate.city, state=candidate.state, population=candidate.population, source="system", brand_id=None, collection_market_id=None, is_active=True)
    row.update(overrides)
    return row


def test_migration_frozen_rule_matches_the_app_rule():
    migration = _migration()
    rows = [_row(candidate) for candidate in _candidates()]
    assert migration.select_panel(rows, 20) == select_panel(_candidates(), 20)
    assert migration.DIVISION_BY_STATE == DIVISION_BY_STATE
    assert migration.PANEL_SIZE == 20


def test_migration_data_step_ranks_active_system_rows_and_deactivates_the_rest_only():
    migration = _migration()
    candidates = _candidates()
    rows = [_row(candidate) for candidate in candidates]
    rows += [
        _row(PanelCandidate("local-big", "Mesa", "AZ", 9_000_000), source="local"),
        _row(PanelCandidate("national", "-", "-", None), source="national"),
        _row(PanelCandidate("adhoc", "Boise", "ID", 9_500_000), source="adhoc", brand_id="b1"),
        _row(PanelCandidate("member", "Yonkers", "NY", 9_900_000), collection_market_id="m-0"),
        _row(PanelCandidate("was-off", "Huge", "CA", 9_999_999), is_active=False),
    ]
    ranks, deactivate = migration.panel_data_step(rows)
    assert ranks == select_panel(candidates, 20)
    assert set(deactivate) == {candidate.id for candidate in candidates} - set(ranks)
    assert len(deactivate) == len(candidates) - 20
    assert not {"local-big", "national", "adhoc", "member", "was-off"} & (set(ranks) | set(deactivate))


def test_migration_data_step_on_an_empty_table_is_a_no_op():
    assert _migration().panel_data_step([]) == ({}, [])


##############################
# PROMOTION
##############################


def test_promotion_refills_a_vacated_division_slot_from_the_same_division():
    full = select_panel(_candidates(), 20)
    by_id = {candidate.id: candidate for candidate in _candidates()}
    boston_id = next(candidate_id for candidate_id, rank in full.items() if by_id[candidate_id].city == "Boston")
    members = [(by_id[candidate_id], rank) for candidate_id, rank in full.items() if candidate_id != boston_id]
    pool = [candidate for candidate in _candidates() if candidate.id not in full]  # Seattle, Denver, OKC, Omaha, Providence...
    assert promotion_plan(members, pool, 20) == {next(c.id for c in pool if c.city == "Providence"): full[boston_id]}


def test_promotion_refills_a_vacated_nationwide_slot_with_the_largest_candidate():
    full = select_panel(_candidates(), 20)
    by_id = {candidate.id: candidate for candidate in _candidates()}
    dallas_id = next(candidate_id for candidate_id, rank in full.items() if by_id[candidate_id].city == "Dallas")
    members = [(by_id[candidate_id], rank) for candidate_id, rank in full.items() if candidate_id != dallas_id]
    pool = [candidate for candidate in _candidates() if candidate.id not in full]
    assert promotion_plan(members, pool, 20) == {next(c.id for c in pool if c.city == "Seattle"): full[dallas_id]}
    assert promotion_plan([(by_id[candidate_id], rank) for candidate_id, rank in full.items()], pool, 20) == {}
    assert promotion_plan(members, [], 20) == {}


##############################
# SMALL PANEL + RE-ASK CITIES
##############################


def _market(market_id, source="system", panel_rank=None, population=0, brand_id=None, collection_market_id=None):
    return SimpleNamespace(id=market_id, city=market_id, state="NY", source=source, panel_rank=panel_rank, population=population, brand_id=brand_id, collection_market_id=collection_market_id)


def test_small_panel_is_nationwide_plus_top_ranked_cities_for_national_and_home_cities_for_local():
    markets = [_market("nationwide", source="national")] + [_market(f"r{rank}", panel_rank=rank) for rank in (3, 1, 7, 2, 5, 4, 6)]
    assert [market.id for market in small_panel(markets, "national", 5)] == ["nationwide", "r1", "r2", "r3", "r4", "r5"]
    homes = [_market("home", source="local"), _market("near", source="local")]
    assert small_panel(homes, "local", 1) == homes


def test_reask_cities_are_the_next_largest_non_panel_system_cities():
    pool = [
        _market("panel", panel_rank=1, population=9_000_000),
        _market("big", population=800_000),
        _market("bigger", population=900_000),
        _market("small", population=100_000),
        _market("local", source="local", population=5_000_000),
        _market("member", population=7_000_000, collection_market_id="big"),
        _market("adhoc", source="adhoc", population=6_000_000, brand_id="b1"),
    ]
    assert [market.id for market in reask_cities(pool, 2)] == ["bigger", "big"]
    assert [market.id for market in reask_cities(pool, 10)] == ["bigger", "big", "small"]
