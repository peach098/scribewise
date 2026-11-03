"""Per-user LLM budget (issue #74): the ``skip_category`` value ``budget`` and the
``activity_kind`` value ``budget_paused``.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-31

A turn that arrives while the user's daily hard cap is reached is skipped without a model call
with the category ``budget`` and listed in ``/skipped`` like other skipped turns. The once-a-day
notice in the control chat is remembered as an ``activity_records`` row of kind
``budget_paused`` (meta: the UTC day), so a restart never sends it twice. The values are added
in autocommit mode, as in 0008, 0012 and 0014: Postgres refuses to use an enum value in the
transaction that added it. No rows are written.

The downgrade cannot drop enum values (Postgres has no ``ALTER TYPE ... DROP VALUE``), so it
clears the gate decision of every turn skipped as ``budget`` (as 0014 does for ``forwarded``;
the ``skip_has_category`` check needs ``skip_category`` and ``reply_decision`` cleared
together) and deletes the ``budget_paused`` activity rows: older code does not know the values
and could not load those rows. The turns stay ``skipped``.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE skip_category ADD VALUE IF NOT EXISTS 'budget'")
        op.execute("ALTER TYPE activity_kind ADD VALUE IF NOT EXISTS 'budget_paused'")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        "UPDATE conversation_turns SET skip_category = NULL, reply_decision = NULL,"
        " decision_source = NULL, decision_confidence = NULL"
        " WHERE skip_category = 'budget'"
    )
    op.execute("DELETE FROM activity_records WHERE kind = 'budget_paused'")
    # The values stay in their types: Postgres has no ALTER TYPE ... DROP VALUE. The upgrade adds
    # them with IF NOT EXISTS, so upgrading again is safe.
