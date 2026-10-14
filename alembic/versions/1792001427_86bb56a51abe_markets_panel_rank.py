"""markets panel rank

Lean collection (owner decision 2026-10-14): national brands collect in ONE
shared panel of 20 system cities + the nationwide pseudo-market instead of all
~150 seeded system markets. Adds markets.panel_rank, then a DATA STEP for
existing DBs: ranks the panel over the active shared system collector rows
(largest city per US Census division, ranked by population, then the next
largest nationwide), deactivates the other system rows (kept — history and
corpus rows stay intact), and creates the nationwide row if missing.

The panel rule is a FROZEN copy of utils/city_panel.select_panel (migrations
never import app code; tests/test_city_panel.py pins the two together).

Downgrade drops the column, reactivates every system row, and deactivates
(never deletes — corpus rows reference it) the nationwide row.

Revision ID: 86bb56a51abe
Revises: 2825f055f18c
Create Date: 2026-10-14 12:00:00.000000

"""

import uuid

import sqlalchemy as sa

from alembic import op

revision = "86bb56a51abe"
down_revision = "2825f055f18c"
branch_labels = None
depends_on = None

PANEL_SIZE = 20

_DIVISION_STATES = {
    "New England": ("CT", "ME", "MA", "NH", "RI", "VT"),
    "Middle Atlantic": ("NJ", "NY", "PA"),
    "East North Central": ("IL", "IN", "MI", "OH", "WI"),
    "West North Central": ("IA", "KS", "MN", "MO", "NE", "ND", "SD"),
    "South Atlantic": ("DE", "DC", "FL", "GA", "MD", "NC", "SC", "VA", "WV"),
    "East South Central": ("AL", "KY", "MS", "TN"),
    "West South Central": ("AR", "LA", "OK", "TX"),
    "Mountain": ("AZ", "CO", "ID", "MT", "NV", "NM", "UT", "WY"),
    "Pacific": ("AK", "CA", "HI", "OR", "WA"),
}
DIVISION_BY_STATE = {state: division for division, states in _DIVISION_STATES.items() for state in states}


def select_panel(rows: list[dict], size: int = PANEL_SIZE) -> dict[str, int]:
    """{id: rank} over rows {id, city, state, population} (frozen copy)."""
    ranked = sorted(rows, key=lambda row: (-(row["population"] or 0), (row["city"] or "").lower(), row["id"]))
    winners, covered = [], set()
    for row in ranked:
        division = DIVISION_BY_STATE.get((row["state"] or "").strip().upper())
        if division and division not in covered:
            covered.add(division)
            winners.append(row)
    chosen = winners[:size]
    chosen_ids = {row["id"] for row in chosen}
    for row in ranked:
        if len(chosen) >= size:
            break
        if row["id"] not in chosen_ids:
            chosen.append(row)
            chosen_ids.add(row["id"])
    return {row["id"]: rank for rank, row in enumerate(chosen, start=1)}


def panel_data_step(rows: list[dict], size: int = PANEL_SIZE) -> tuple[dict[str, int], list[str]]:
    """PURE: markets rows {id, city, state, population, source, brand_id,
    collection_market_id, is_active} -> ({id: panel rank}, ids to deactivate).
    Candidates = ACTIVE shared system collector rows; the unchosen candidates
    are deactivated. Every other row is left alone."""
    candidates = [row for row in rows if row["source"] == "system" and row["brand_id"] is None and row["collection_market_id"] is None and row["is_active"]]
    ranks = select_panel(candidates, size)
    deactivate = sorted(row["id"] for row in candidates if row["id"] not in ranks)
    return ranks, deactivate


def upgrade() -> None:
    op.add_column("markets", sa.Column("panel_rank", sa.Integer(), nullable=True))

    connection = op.get_bind()
    rows = [dict(row._mapping) for row in connection.execute(sa.text("SELECT id, city, state, population, source, brand_id, collection_market_id, is_active FROM markets"))]
    ranks, deactivate = panel_data_step(rows)
    for market_id, rank in ranks.items():
        connection.execute(sa.text("UPDATE markets SET panel_rank = :rank, is_active = true WHERE id = :id"), {"rank": rank, "id": market_id})
    for market_id in deactivate:
        connection.execute(sa.text("UPDATE markets SET is_active = false WHERE id = :id"), {"id": market_id})

    has_nationwide = connection.execute(sa.text("SELECT 1 FROM markets WHERE source = 'national' AND brand_id IS NULL LIMIT 1")).first() is not None
    if not has_nationwide:
        connection.execute(sa.text("INSERT INTO markets (id, city, state, source, is_active) VALUES (:id, '-', '-', 'national', true) ON CONFLICT DO NOTHING"), {"id": str(uuid.uuid4())})


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("UPDATE markets SET is_active = true WHERE source = 'system'"))
    connection.execute(sa.text("UPDATE markets SET is_active = false WHERE source = 'national'"))
    op.drop_column("markets", "panel_rank")
