"""Per-user daily LLM spend (issue #67): ``llm_spend``.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-31

One row per (user, UTC day, route) with the call count, token counts and ``cost_usd``, upserted
by the LLM router after each billed call. Counts and money only, no message content, so the
table is not encrypted. ``user_id`` cascades from ``users``: deleting an account removes its
spend, like every other per-user table. No rows are written here; spend before the upgrade is
not backfilled.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COUNTERS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "cache_write_1h_input_tokens",
)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "llm_spend",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("route", sa.Text(), nullable=False),
        sa.Column("calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_calls", sa.Integer(), server_default="0", nullable=False),
        *(
            sa.Column(name, sa.BigInteger(), server_default="0", nullable=False)
            for name in _COUNTERS
        ),
        sa.Column("cost_usd", sa.Numeric(14, 6), server_default="0", nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_llm_spend_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_spend")),
        sa.UniqueConstraint("user_id", "day", "route", name=op.f("uq_llm_spend_user_id_day_route")),
    )
    op.create_index("ix_llm_spend_day", "llm_spend", ["day"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_llm_spend_day", table_name="llm_spend")
    op.drop_table("llm_spend")
