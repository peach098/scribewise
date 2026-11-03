"""Drop the group drafting mode (specs/006-group-mention-only data-model.md, research R4):
work_chats.group_mode and the group_mode enum type.

Revision ID: 0009
Revises: 0011
Create Date: 2026-10-23

Every group now follows one rule: a draft only when the user is tagged or replied to. No row
update is needed before the drop: with the column gone, chats that were set to ``always``
follow the rule like every other group (FR-005).

Deploy: the previous release reads ``work_chats.group_mode``, so stop the app, run
``alembic upgrade head``, then start the new image.

Downgrade recreates the column NOT NULL with every chat at ``mentions_only``. It cannot restore
which chats were ``always``; that loss is accepted, since the mode is retired.

Written in parallel with 0011 and 0012, which reached main first; re-chained onto 0011 (the
then-latest revision) so that ``alembic heads`` shows exactly one head. It has no data
dependency on its neighbours.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column("work_chats", "group_mode")
    sa.Enum(name="group_mode").drop(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    """Downgrade schema."""
    group_mode = sa.Enum("mentions_only", "always", name="group_mode")
    group_mode.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "work_chats",
        sa.Column("group_mode", group_mode, nullable=False, server_default="mentions_only"),
    )
    op.alter_column("work_chats", "group_mode", server_default=None)
