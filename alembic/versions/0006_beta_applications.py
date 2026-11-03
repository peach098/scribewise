"""Beta applications from the bot (005 US5, data-model.md "beta_applications").

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-22

Adds ``beta_applications`` (keyed by the applicant's Telegram id, no FK to ``users``: an
applicant has no account) and three ``access_event_kind`` values. Postgres cannot drop enum
values, so ``downgrade()`` leaves them (unused) and drops only the table and its status enum;
``ADD VALUE IF NOT EXISTS`` makes a later upgrade work again.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_EVENT_KINDS = ("application_submitted", "application_approved", "application_rejected")

BETA_APPLICATION_STATUS = postgresql.ENUM(
    "pending",
    "approved",
    "rejected",
    name="beta_application_status",
    create_type=False,
)


def upgrade() -> None:
    """Upgrade schema."""
    for value in NEW_EVENT_KINDS:
        # Allowed in a transaction since Postgres 12; the values are not used here.
        op.execute(f"ALTER TYPE access_event_kind ADD VALUE IF NOT EXISTS '{value}'")
    BETA_APPLICATION_STATUS.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "beta_applications",
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.Text(), nullable=True),
        sa.Column("use_case", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("consent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", BETA_APPLICATION_STATUS, nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "operator_messages",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(use_case) <= 1000", name=op.f("ck_beta_applications_use_case_length")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_beta_applications")),
    )
    op.create_index(op.f("ix_beta_applications_telegram_id"), "beta_applications", ["telegram_id"])
    op.create_index(
        "uq_beta_applications_pending",
        "beta_applications",
        ["telegram_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("uq_beta_applications_pending", table_name="beta_applications")
    op.drop_index(op.f("ix_beta_applications_telegram_id"), table_name="beta_applications")
    op.drop_table("beta_applications")
    BETA_APPLICATION_STATUS.drop(op.get_bind(), checkfirst=True)
