"""brand approval preset

Revision ID: c7015f13f1f5
Revises: a5e90c8a9c09
Create Date: 2026-09-02 15:55:44.378852

"""

import sqlalchemy as sa

from alembic import op

revision = "c7015f13f1f5"
down_revision = "a5e90c8a9c09"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("brands", sa.Column("approval_preset", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("brands", "approval_preset")
