"""users recap seen week

The weekly recap card's dismissal: the Monday of the ISO week whose recap the
user marked seen (POST /brands/{id}/recap/seen). NULL = never dismissed one.

Revision ID: d63a8fa09d7e
Revises: 531ec04076d3
Create Date: 2026-10-14 07:14:07.000000

"""

import sqlalchemy as sa

from alembic import op

revision = "d63a8fa09d7e"
down_revision = "531ec04076d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("recap_seen_week", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "recap_seen_week")
