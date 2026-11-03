"""Search over earlier conversations (specs/004-conversation-search): users.search_earlier_messages.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-22

On by default (FR-S16); existing users get ``true``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "users",
        sa.Column(
            "search_earlier_messages", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("users", "search_earlier_messages")
