"""Skip messages that need no reply (specs/004-skip-closing-messages data-model.md):
reply-gate decision columns on conversation_turns, skip switches on users / work_chats,
drafts.possibly_unnecessary.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-21

Postgres refuses to use a value added to an existing enum in the transaction that added it,
and the partial index ``ix_turns_user_skipped ... WHERE state = 'skipped'`` is such a use
(research R5). The new values of ``turn_state`` and ``activity_kind`` are therefore added
first, in autocommit mode, and only then are the new types, columns, constraints and the index
created in the normal migration transaction. No rows are written.

Revision 0008 follows 0007 (0007_search_earlier, specs/004-conversation-search); it was
renumbered from 0005 when main gained 0005_access_gate, 0006 and 0007.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REPLY_DECISION = sa.Enum("draft", "skip", name="reply_decision")
SKIP_CATEGORY = sa.Enum("thanks", "farewell", "acknowledgment", name="skip_category")
DECISION_SOURCE = sa.Enum(
    "pregate", "model", "fallback", "habit", "disabled", "user", name="decision_source"
)
NEW_TYPES = (REPLY_DECISION, SKIP_CATEGORY, DECISION_SOURCE)


def upgrade() -> None:
    """Upgrade schema."""
    # First, committed before anything uses them (see the module docstring).
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE turn_state ADD VALUE IF NOT EXISTS 'skipped'")
        op.execute("ALTER TYPE activity_kind ADD VALUE IF NOT EXISTS 'skipped'")
        op.execute("ALTER TYPE activity_kind ADD VALUE IF NOT EXISTS 'draft_anyway'")

    bind = op.get_bind()
    for enum_type in NEW_TYPES:
        enum_type.create(bind, checkfirst=True)

    op.add_column("conversation_turns", sa.Column("reply_decision", REPLY_DECISION, nullable=True))
    op.add_column("conversation_turns", sa.Column("skip_category", SKIP_CATEGORY, nullable=True))
    op.add_column(
        "conversation_turns", sa.Column("decision_source", DECISION_SOURCE, nullable=True)
    )
    op.add_column(
        "conversation_turns",
        sa.Column("decision_confidence", sa.Numeric(precision=3, scale=2), nullable=True),
    )
    op.add_column(
        "conversation_turns",
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "conversation_turns",
        sa.Column("draft_anyway_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_conversation_turns_skip_has_category"),
        "conversation_turns",
        "reply_decision <> 'skip' OR skip_category IS NOT NULL",
    )
    op.create_check_constraint(
        op.f("ck_conversation_turns_decision_confidence_range"),
        "conversation_turns",
        "decision_confidence IS NULL OR decision_confidence BETWEEN 0 AND 1",
    )
    op.create_index(
        "ix_turns_user_skipped",
        "conversation_turns",
        ["user_id", sa.text("decided_at DESC")],
        unique=False,
        postgresql_where=sa.text("state = 'skipped'"),
    )

    op.add_column(
        "users",
        sa.Column("skip_closing", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.add_column("work_chats", sa.Column("skip_closing", sa.Boolean(), nullable=True))
    op.add_column(
        "drafts",
        sa.Column(
            "possibly_unnecessary", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("drafts", "possibly_unnecessary")
    op.drop_column("work_chats", "skip_closing")
    op.drop_column("users", "skip_closing")
    op.drop_index(
        "ix_turns_user_skipped",
        table_name="conversation_turns",
        postgresql_where=sa.text("state = 'skipped'"),
    )
    op.drop_constraint(
        op.f("ck_conversation_turns_decision_confidence_range"), "conversation_turns", type_="check"
    )
    op.drop_constraint(
        op.f("ck_conversation_turns_skip_has_category"), "conversation_turns", type_="check"
    )
    for column in (
        "draft_anyway_at",
        "decided_at",
        "decision_confidence",
        "decision_source",
        "skip_category",
        "reply_decision",
    ):
        op.drop_column("conversation_turns", column)
    bind = op.get_bind()
    for enum_type in reversed(NEW_TYPES):
        enum_type.drop(bind, checkfirst=True)
    # 'skipped' stays in turn_state and 'skipped' / 'draft_anyway' stay in activity_kind:
    # Postgres has no ALTER TYPE ... DROP VALUE. The upgrade adds them with IF NOT EXISTS.
