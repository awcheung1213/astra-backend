"""prompt_results: add text/raw, rename mentioned -> brand_mentioned

Revision ID: c8d4e5f60a17
Revises: b7e0d1a92c3e
Create Date: 2026-08-25 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'c8d4e5f60a17'
down_revision = 'b7e0d1a92c3e'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('prompt_results', sa.Column('text', sa.Text(), server_default='', nullable=False))
    op.add_column('prompt_results', sa.Column('raw', sa.Text(), server_default='', nullable=False))
    op.alter_column('prompt_results', 'mentioned', new_column_name='brand_mentioned')


def downgrade() -> None:
    op.alter_column('prompt_results', 'brand_mentioned', new_column_name='mentioned')
    op.drop_column('prompt_results', 'raw')
    op.drop_column('prompt_results', 'text')
