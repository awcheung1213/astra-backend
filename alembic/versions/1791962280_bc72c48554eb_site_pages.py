"""site pages

The brand's true page list (sitemap, else link crawl) + the audit's rotation
state: each run checks at most MAX_PAGES pages, oldest-checked first
(worker/tasks/site_audit.py). One row per (brand, normalized url).

Revision ID: bc72c48554eb
Revises: d63a8fa09d7e
Create Date: 2026-10-14 00:18:00.000000

"""

import sqlalchemy as sa

from alembic import op

revision = "bc72c48554eb"
down_revision = "d63a8fa09d7e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "site_pages",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("brand_id", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("section", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default="unchecked", nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["brand_id"], ["brands.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("brand_id", "url"),
    )
    op.create_index("ix_site_pages_brand_id_last_checked_at", "site_pages", ["brand_id", "last_checked_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_site_pages_brand_id_last_checked_at", table_name="site_pages")
    op.drop_table("site_pages")
