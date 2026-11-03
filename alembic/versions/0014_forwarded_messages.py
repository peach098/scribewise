"""Forwarded messages (issue #47): ``messages.is_forwarded``, the encrypted origin label
``messages.forwarded_from``, the ``skip_category`` value ``forwarded`` (a turn that holds
only forwarded messages is skipped without a model call and listed in ``/skipped``) and
``conversation_turns.opening_message_id`` (a group turn that starts at the sender's forward
before the message that addressed the user: only forwards before it belong to the turn) and
``conversation_turns.forwards_hold_until`` (a forwards-only turn is not due before it).

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-30

``is_forwarded`` is not null with a server default of false, so existing rows (stored before
the flag was read) count as not forwarded. ``forwarded_from`` holds ciphertext written by the
application (``EncryptedText``), like ``messages.text``; it is deleted with the row by the
retention purge. The enum value is added in autocommit mode, as in 0008 and 0012: Postgres
refuses to use an enum value in the transaction that added it. No rows are written.

The downgrade cannot drop the enum value (Postgres has no ``ALTER TYPE ... DROP VALUE``), so it
clears the gate decision of every turn that uses it (``skip_category``, ``reply_decision``,
``decision_source``, ``decision_confidence``; the ``skip_has_category`` check needs both of the
first two cleared together): older code does not know the value and could not load the row.
Those turns stay ``skipped``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE skip_category ADD VALUE IF NOT EXISTS 'forwarded'")
    op.add_column(
        "messages",
        sa.Column("is_forwarded", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("messages", sa.Column("forwarded_from", sa.Text(), nullable=True))
    op.add_column("conversation_turns", sa.Column("opening_message_id", sa.UUID(), nullable=True))
    op.add_column(
        "conversation_turns",
        sa.Column("forwards_hold_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_conversation_turns_opening_message_id_messages"),
        "conversation_turns",
        "messages",
        ["opening_message_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        "UPDATE conversation_turns SET skip_category = NULL, reply_decision = NULL,"
        " decision_source = NULL, decision_confidence = NULL"
        " WHERE skip_category = 'forwarded'"
    )
    op.drop_constraint(
        op.f("fk_conversation_turns_opening_message_id_messages"),
        "conversation_turns",
        type_="foreignkey",
    )
    op.drop_column("conversation_turns", "forwards_hold_until")
    op.drop_column("conversation_turns", "opening_message_id")
    op.drop_column("messages", "forwarded_from")
    op.drop_column("messages", "is_forwarded")
    # 'forwarded' stays in skip_category: Postgres has no ALTER TYPE ... DROP VALUE. The upgrade
    # adds it with IF NOT EXISTS, so upgrading again is safe.
