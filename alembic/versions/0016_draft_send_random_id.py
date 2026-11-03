"""A per-draft idempotency key for sending (issue #54): ``drafts.send_random_id``.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-31

The MTProto ``random_id`` of the draft's ``messages.sendMessage``. ``sending/send.py`` stores it
(committed) before the first send attempt and passes the same value on every retry, so Telegram
deduplicates a retry after an ambiguous failure instead of delivering the reply twice. Nullable:
existing drafts get NULL and receive a key on their first send attempt. No rows are written
here; adding a nullable column without a default does not rewrite the table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("drafts", sa.Column("send_random_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("drafts", "send_random_id")
