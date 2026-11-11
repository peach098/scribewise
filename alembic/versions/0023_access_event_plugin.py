"""A generic ``plugin`` value of ``access_event_kind``.

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-11

Plugins record their own access events with ``kind = 'plugin'`` and the event name in
``meta_json.event`` (docs/plugins.md). Postgres cannot drop enum values, so ``downgrade()``
leaves it (unused); ``ADD VALUE IF NOT EXISTS`` makes a later upgrade work again.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0023"
down_revision: str | Sequence[str] | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Allowed in a transaction since Postgres 12; the value is not used here.
    op.execute("ALTER TYPE access_event_kind ADD VALUE IF NOT EXISTS 'plugin'")


def downgrade() -> None:
    pass  # enum values cannot be dropped; 'plugin' stays, unused
