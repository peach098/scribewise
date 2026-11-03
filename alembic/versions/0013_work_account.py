"""Work account mode (specs/008-work-account-mode data-model.md): ``users.work_account`` (+
``work_account_changed_at``), ``work_chats.origin`` / ``work_chats.excluded`` with the check
``origin = 'auto' OR NOT excluded``, the new ``activity_kind`` and ``dropped_reason`` values.

Revision ID: 0013
Revises: 0010
Create Date: 2026-10-23

The enum values are added first, in autocommit mode, as in 0008: Postgres refuses to use an
enum value in the transaction that added it. A chat picked up by the mode is recorded with
007's ``chats_added`` kind (meta ``origin: "auto"``), so no ``chat_added`` value is added.
Existing chats become ``origin = 'picked'``, ``excluded = false``; existing users have the mode
off. No row is rewritten.

The chain is 0008 -> 0012 -> 0011 -> 0009 -> 0010 -> 0013: 0013 was written on top of 0012
(specs/007) and re-chained onto 0010 (specs/009) at merge time, so ``alembic heads`` shows one head.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WORK_CHAT_ORIGIN = sa.Enum("picked", "auto", name="work_chat_origin")
NEW_ACTIVITY_KINDS = ("work_account_on", "work_account_off", "chat_excluded", "chat_included")


def upgrade() -> None:
    """Upgrade schema."""
    with op.get_context().autocommit_block():
        for value in NEW_ACTIVITY_KINDS:
            op.execute(f"ALTER TYPE activity_kind ADD VALUE IF NOT EXISTS '{value}'")
        op.execute("ALTER TYPE dropped_reason ADD VALUE IF NOT EXISTS 'work_account_off'")

    WORK_CHAT_ORIGIN.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "work_chats",
        sa.Column("origin", WORK_CHAT_ORIGIN, server_default="picked", nullable=False),
    )
    op.add_column(
        "work_chats",
        sa.Column("excluded", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_work_chats_excluded_auto_only"),
        "work_chats",
        "origin = 'auto' OR NOT excluded",
    )
    op.add_column(
        "users",
        sa.Column("work_account", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "users", sa.Column("work_account_changed_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("users", "work_account_changed_at")
    op.drop_column("users", "work_account")
    op.drop_constraint(op.f("ck_work_chats_excluded_auto_only"), "work_chats", type_="check")
    op.drop_column("work_chats", "excluded")
    op.drop_column("work_chats", "origin")
    WORK_CHAT_ORIGIN.drop(op.get_bind(), checkfirst=True)
    # The activity_kind / dropped_reason values stay: Postgres has no ALTER TYPE ... DROP VALUE.
    # The upgrade adds them with IF NOT EXISTS, so upgrading again is safe.
