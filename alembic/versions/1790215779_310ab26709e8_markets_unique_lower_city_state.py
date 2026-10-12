"""markets unique lower city state

One markets row per physical city (rule 3), case-insensitively: replaces the
case-sensitive UNIQUE (city, state), which the lower() index strictly
supersedes. Fails loudly if case-variant duplicates already exist.

Revision ID: 310ab26709e8
Revises: 9f83fec900f7
Create Date: 2026-09-23 19:09:39.000000

"""

import sqlalchemy as sa

from alembic import op

revision = "310ab26709e8"
down_revision = "9f83fec900f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("uq_markets_lower_city_state", "markets", [sa.text("lower(city)"), sa.text("lower(state)")], unique=True)
    op.drop_constraint("markets_city_state_key", "markets", type_="unique")


def downgrade() -> None:
    op.create_unique_constraint("markets_city_state_key", "markets", ["city", "state"])
    op.drop_index("uq_markets_lower_city_state", table_name="markets")
