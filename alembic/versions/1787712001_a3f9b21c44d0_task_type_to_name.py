"""tasks.type -> tasks.name

The ix_tasks_live partial index tracks the renamed column automatically.

Revision ID: a3f9b21c44d0
Revises: 8c6fec0d5875
Create Date: 2026-08-25 00:00:00.000000

"""
from alembic import op

revision = 'a3f9b21c44d0'
down_revision = '8c6fec0d5875'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column('tasks', 'type', new_column_name='name')


def downgrade() -> None:
    op.alter_column('tasks', 'name', new_column_name='type')
