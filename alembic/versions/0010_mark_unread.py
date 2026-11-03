"""Mark chats unread after agent activity (specs/009-mark-chats-unread data-model.md):
``users.mark_unread`` (per-user switch, on for everyone) and ``work_chats.unread_mark_floor``
(newest message id the chat was marked for or the user is known to have seen).

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-23

Written in parallel with 0009, 0011 and 0012, which reached main first; re-chained onto 0009
(the then-latest revision) so that ``alembic heads`` shows exactly one head. It has no data
dependency on its neighbours. No rows are written; existing users get the server
default ``true`` and existing work chats a NULL floor.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "users",
        sa.Column("mark_unread", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.add_column("work_chats", sa.Column("unread_mark_floor", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        op.f("ck_work_chats_unread_mark_floor_positive"),
        "work_chats",
        "unread_mark_floor IS NULL OR unread_mark_floor > 0",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        op.f("ck_work_chats_unread_mark_floor_positive"), "work_chats", type_="check"
    )
    op.drop_column("work_chats", "unread_mark_floor")
    op.drop_column("users", "mark_unread")
