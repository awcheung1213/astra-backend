"""prompts topic

A short plain label per tracked question ("Emergency plumber") for the AI
answers tile grid. Nullable: research stamps it on new rows; existing rows
stay NULL until the backfill_question_topics task fills them (the FE falls
back to the question's first words meanwhile).

Revision ID: 531ec04076d3
Revises: 310ab26709e8
Create Date: 2026-10-14 07:15:00.000000

"""

import sqlalchemy as sa

from alembic import op

revision = "531ec04076d3"
down_revision = "310ab26709e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("prompts", sa.Column("topic", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("prompts", "topic")
