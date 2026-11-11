"""The automatic profile refresh switch: ``users.profile_auto_refresh``.

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-11

Nullable, NULL for every existing user: they follow ``SCRIBE_PROFILE_AUTO_REFRESH_DEFAULT``
(true by default, so nothing changes until the user or the operator chooses otherwise).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0022"
down_revision: str | Sequence[str] | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("profile_auto_refresh", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "profile_auto_refresh")
