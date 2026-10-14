"""question tiers

The automatic question funnel (utils/question_tiers.py): prompts gain a
level (core | watching | suggested), an owner pin, the plain-words reason for
the latest move, when it moved, when collection last picked it, and the
sorter's pending (unconfirmed) signal. question_moves keeps the move history
the weekly recap and the monthly swap budget read.

Data step (per brand, non-retired rows only):
  - active questions -> core, the first CORE_SLOTS (5) by research rank
    (created_at); any further active ones -> watching
  - inactive questions -> watching, newest research run first (rank order
    inside a run), until the brand has 40 watching; the rest -> suggested
  - is_active is re-derived as tier = 'core' (it mirrors the tiles from now on;
    collection reads questions_to_collect, never is_active)
Retired rows stay 'suggested' (the column default). tier_changed_at stays NULL
for migrated rows (they did not "move this month").

Revision ID: 2825f055f18c
Revises: bc72c48554eb
Create Date: 2026-10-14 12:00:00.000000

"""

import sqlalchemy as sa

from alembic import op

revision = "2825f055f18c"
down_revision = "bc72c48554eb"
branch_labels = None
depends_on = None

CORE_SLOTS = 5  # = utils.question_tiers.CORE_SLOTS at the time of writing
WATCHING_POOL = 40  # = utils.question_tiers.WATCHING_POOL_TARGET at the time of writing


def upgrade() -> None:
    op.add_column("prompts", sa.Column("tier", sa.String(), server_default="suggested", nullable=False))
    op.add_column("prompts", sa.Column("pinned", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("prompts", sa.Column("tier_reason", sa.Text(), nullable=True))
    op.add_column("prompts", sa.Column("tier_changed_at", sa.DateTime(), nullable=True))
    op.add_column("prompts", sa.Column("last_checked_at", sa.DateTime(), nullable=True))
    op.add_column("prompts", sa.Column("pending_signal", sa.String(), nullable=True))
    op.create_index("ix_prompts_brand_id_tier", "prompts", ["brand_id", "tier"], unique=False)

    op.create_table(
        "question_moves",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("brand_id", sa.String(), nullable=False),
        sa.Column("prompt_id", sa.String(), nullable=False),
        sa.Column("from_tier", sa.String(), nullable=False),
        sa.Column("to_tier", sa.String(), nullable=False),
        sa.Column("reason_code", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("automatic", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["brand_id"], ["brands.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["prompt_id"], ["prompts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_question_moves_brand_id_created_at", "question_moves", ["brand_id", "created_at"], unique=False)
    op.create_index("ix_question_moves_prompt_id", "question_moves", ["prompt_id"], unique=False)

    # active -> core (top CORE_SLOTS by research rank), overflow -> watching
    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT id, row_number() OVER (PARTITION BY brand_id ORDER BY created_at, id) AS rank
                FROM prompts WHERE is_active AND retired_at IS NULL
            )
            UPDATE prompts SET tier = CASE WHEN ranked.rank <= :core_slots THEN 'core' ELSE 'watching' END
            FROM ranked WHERE prompts.id = ranked.id
            """
        ).bindparams(core_slots=CORE_SLOTS)
    )
    # inactive -> watching up to WATCHING_POOL per brand (counting the overflow above):
    # newest research run first (runs are minutes+ apart), rank order inside a run
    op.execute(
        sa.text(
            """
            WITH overflow AS (
                SELECT brand_id, count(*) AS watching FROM prompts WHERE tier = 'watching' AND retired_at IS NULL GROUP BY brand_id
            ),
            ranked AS (
                SELECT id, brand_id, row_number() OVER (PARTITION BY brand_id ORDER BY date_trunc('minute', created_at) DESC, created_at, id) AS rank
                FROM prompts WHERE NOT is_active AND retired_at IS NULL
            )
            UPDATE prompts SET tier = 'watching'
            FROM ranked LEFT JOIN overflow ON overflow.brand_id = ranked.brand_id
            WHERE prompts.id = ranked.id AND ranked.rank <= :pool - COALESCE(overflow.watching, 0)
            """
        ).bindparams(pool=WATCHING_POOL)
    )
    op.execute("UPDATE prompts SET is_active = (tier = 'core')")


def downgrade() -> None:
    # is_active goes back to "collected" (core + watching), the closest old meaning
    op.execute("UPDATE prompts SET is_active = (tier IN ('core', 'watching') AND retired_at IS NULL)")
    op.drop_index("ix_question_moves_prompt_id", table_name="question_moves")
    op.drop_index("ix_question_moves_brand_id_created_at", table_name="question_moves")
    op.drop_table("question_moves")
    op.drop_index("ix_prompts_brand_id_tier", table_name="prompts")
    op.drop_column("prompts", "pending_signal")
    op.drop_column("prompts", "last_checked_at")
    op.drop_column("prompts", "tier_changed_at")
    op.drop_column("prompts", "tier_reason")
    op.drop_column("prompts", "pinned")
    op.drop_column("prompts", "tier")
