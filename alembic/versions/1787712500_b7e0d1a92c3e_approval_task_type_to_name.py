"""approval_settings.task_type -> task_name (matches tasks.name)

Revision ID: b7e0d1a92c3e
Revises: a3f9b21c44d0
Create Date: 2026-08-25 00:00:00.000000

"""
from alembic import op

revision = 'b7e0d1a92c3e'
down_revision = 'a3f9b21c44d0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column('approval_settings', 'task_type', new_column_name='task_name')
    op.execute("ALTER TABLE approval_settings RENAME CONSTRAINT approval_settings_brand_id_task_type_key TO approval_settings_brand_id_task_name_key")


def downgrade() -> None:
    op.execute("ALTER TABLE approval_settings RENAME CONSTRAINT approval_settings_brand_id_task_name_key TO approval_settings_brand_id_task_type_key")
    op.alter_column('approval_settings', 'task_name', new_column_name='task_type')
