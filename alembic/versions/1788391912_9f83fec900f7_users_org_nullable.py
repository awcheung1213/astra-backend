"""users org nullable

NULL organization_id = JIT-provisioned from a Supabase token but signup not
completed yet (the onboarding gate).

Revision ID: 9f83fec900f7
Revises: c7015f13f1f5
Create Date: 2026-09-02 23:31:52.483246

"""

import sqlalchemy as sa

from alembic import op

revision = "9f83fec900f7"
down_revision = "c7015f13f1f5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("users", "organization_id", existing_type=sa.VARCHAR(), nullable=True)


def downgrade() -> None:
    op.alter_column("users", "organization_id", existing_type=sa.VARCHAR(), nullable=False)
