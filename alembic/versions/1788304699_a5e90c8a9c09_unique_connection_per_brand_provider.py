"""unique connection per brand provider

Revision ID: a5e90c8a9c09
Revises: c8d4e5f60a17
Create Date: 2026-09-01 23:18:19.182016

"""

from alembic import op

revision = "a5e90c8a9c09"
down_revision = "c8d4e5f60a17"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint("uq_connections_brand_provider", "connections", ["brand_id", "provider"])


def downgrade() -> None:
    op.drop_constraint("uq_connections_brand_provider", "connections", type_="unique")
