"""Control-bot UI language (issue #8): users.language.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-21

``en`` or ``ru``; existing users keep English (the server default) until they switch in
/settings. New users get a default from their Telegram language at /start.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("users", sa.Column("language", sa.Text(), nullable=False, server_default="en"))
    op.create_check_constraint(op.f("ck_users_language"), "users", "language IN ('en', 'ru')")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f("ck_users_language"), "users", type_="check")
    op.drop_column("users", "language")
