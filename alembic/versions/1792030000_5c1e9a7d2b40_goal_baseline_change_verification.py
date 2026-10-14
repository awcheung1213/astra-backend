"""goal baseline + change verification

Marco review round 6 (2026-10-14):
- goals.baseline / baseline_at: the owner's starting number ("money coming in
  per month right now"), answered on the plan's Needs-you card, so the goal
  reads "$X today -> $Y goal" instead of a bare target.
- change_sets.verification: what a live re-read of the page found after an
  apply ({"status": confirmed|not_showing|checking, "checked_at", "attempts"})
  — a receipt only says "Google will see it" once the page actually shows it.

Revision ID: 5c1e9a7d2b40
Revises: 86bb56a51abe
Create Date: 2026-10-14 23:30:00.000000

"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "5c1e9a7d2b40"
down_revision = "86bb56a51abe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("goals", sa.Column("baseline", sa.Numeric(14, 2), nullable=True))
    op.add_column("goals", sa.Column("baseline_at", sa.DateTime(), nullable=True))
    op.add_column("change_sets", sa.Column("verification", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("change_sets", "verification")
    op.drop_column("goals", "baseline_at")
    op.drop_column("goals", "baseline")
