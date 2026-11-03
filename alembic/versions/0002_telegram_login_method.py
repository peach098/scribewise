"""Telegram login method (002 data-model.md): telegram_sessions.login_method / connected_at /
account_label.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-20

Existing sessions were all created by the phone-code login, so they are backfilled with
``phone_code`` and ``connected_at = created_at`` before the columns become ``NOT NULL``;
``account_label`` (encrypted text) stays null for them until the next login.
Postgres cannot remove an enum value, so the downgrade leaves ``telegram_login`` in
``activity_kind`` (a documented no-op); the upgrade adds it with ``IF NOT EXISTS``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LOGIN_METHOD = sa.Enum("phone_code", "qr", name="telegram_login_method")


def upgrade() -> None:
    """Upgrade schema."""
    LOGIN_METHOD.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "telegram_sessions",
        sa.Column("login_method", LOGIN_METHOD, nullable=True),
    )
    op.add_column(
        "telegram_sessions",
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("telegram_sessions", sa.Column("account_label", sa.Text(), nullable=True))
    op.execute(
        "UPDATE telegram_sessions SET login_method = 'phone_code', connected_at = created_at"
    )
    op.alter_column("telegram_sessions", "login_method", nullable=False)
    op.alter_column("telegram_sessions", "connected_at", nullable=False)
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block on older Postgres and the
    # new value cannot be used in the same transaction, so it runs in autocommit mode.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE activity_kind ADD VALUE IF NOT EXISTS 'telegram_login'")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("telegram_sessions", "account_label")
    op.drop_column("telegram_sessions", "connected_at")
    op.drop_column("telegram_sessions", "login_method")
    LOGIN_METHOD.drop(op.get_bind(), checkfirst=True)
    # 'telegram_login' stays in activity_kind: Postgres has no ALTER TYPE ... DROP VALUE.
