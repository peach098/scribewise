"""Behaviour rules and informational skips (specs/013-noncommittal-replies): the
``skip_category`` values ``informational`` and ``user_rule`` and the ``decision_source`` value
``rule``.

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-03

A turn that only informs is skipped with the category ``informational``; a turn a user rule
decided is stored with the source ``rule`` (and the category ``user_rule`` when the rule skipped
it). The values are added in autocommit mode, as in 0008, 0012, 0014 and 0019: Postgres refuses
to use an enum value in the transaction that added it. No rows are written.

The downgrade is a no-op: Postgres has no ``ALTER TYPE ... DROP VALUE``, and clearing the
decisions that use the new values would lose the user's skip history. Older code would fail to
load such rows, so a downgrade after 013 has been running needs those turns cleared by hand.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE skip_category ADD VALUE IF NOT EXISTS 'informational'")
        op.execute("ALTER TYPE skip_category ADD VALUE IF NOT EXISTS 'user_rule'")
        op.execute("ALTER TYPE decision_source ADD VALUE IF NOT EXISTS 'rule'")


def downgrade() -> None:
    """Downgrade schema."""
    # The values stay in their types: Postgres has no ALTER TYPE ... DROP VALUE. The upgrade adds
    # them with IF NOT EXISTS, so upgrading again is safe.
