"""Add work chats after setup (specs/007-add-work-chats data-model.md): the ``activity_kind``
value ``chats_added`` (one record per ➕ Done that added chats; meta holds work chat ids only).

Revision ID: 0012
Revises: 0008
Create Date: 2026-10-23

The value is added in autocommit mode, as in 0008: Postgres refuses to use an enum value in
the transaction that added it. No table, column or row changes.

Revision 0012 revises 0008, like the parallel branches 0009-0011 and 0013; at merge time each
later one is re-chained (``down_revision``) onto the then-latest revision.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE activity_kind ADD VALUE IF NOT EXISTS 'chats_added'")


def downgrade() -> None:
    """Downgrade schema."""
    # 'chats_added' stays in activity_kind: Postgres has no ALTER TYPE ... DROP VALUE. The
    # upgrade adds it with IF NOT EXISTS, so upgrading again is safe.
