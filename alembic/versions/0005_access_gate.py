"""Beta & subscription access gate (005 data-model.md): beta_entries, subscriptions,
refused_contacts, access_events and users.access_suspended_at.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-21

The new tables are keyed by the person's Telegram id (= ``users.telegram_control_chat_id``)
with no FK to ``users``: a person may be invited or refused before any account exists.
Existing users get ``access_suspended_at = NULL`` (not suspended); nothing is backfilled.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACCESS_EVENT_KIND = postgresql.ENUM(
    "beta_added",
    "beta_removed",
    "beta_linked",
    "subscription_granted",
    "subscription_extended",
    "subscription_ended",
    "subscription_reminded",
    "access_lost",
    "access_restored",
    name="access_event_kind",
    create_type=False,
)


def upgrade() -> None:
    """Upgrade schema."""
    ACCESS_EVENT_KIND.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "beta_entries",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("username", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("added_by", sa.Text(), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "telegram_id IS NOT NULL OR username IS NOT NULL",
            name=op.f("ck_beta_entries_id_or_username"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_beta_entries")),
        sa.UniqueConstraint("telegram_id", name=op.f("uq_beta_entries_telegram_id")),
    )
    op.create_index(
        op.f("uq_beta_entries_unlinked_username"),
        "beta_entries",
        ["username"],
        unique=True,
        postgresql_where=sa.text("telegram_id IS NULL"),
    )

    op.create_table(
        "subscriptions",
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("granted_by", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("reminded_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint("ends_at > starts_at", name=op.f("ck_subscriptions_period")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subscriptions")),
    )
    op.create_index(op.f("ix_subscriptions_telegram_id"), "subscriptions", ["telegram_id"])

    op.create_table(
        "refused_contacts",
        sa.Column("telegram_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("telegram_id", name=op.f("pk_refused_contacts")),
    )

    op.create_table(
        "access_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", ACCESS_EVENT_KIND, nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "meta_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_access_events")),
    )
    op.create_index(op.f("ix_access_events_at"), "access_events", ["at"])
    op.create_index(op.f("ix_access_events_telegram_id"), "access_events", ["telegram_id"])

    op.add_column(
        "users", sa.Column("access_suspended_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        op.f("ix_users_access_suspended_at_null"),
        "users",
        ["access_suspended_at"],
        postgresql_where=sa.text("access_suspended_at IS NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_users_access_suspended_at_null"), table_name="users")
    op.drop_column("users", "access_suspended_at")
    op.drop_index(op.f("ix_access_events_telegram_id"), table_name="access_events")
    op.drop_index(op.f("ix_access_events_at"), table_name="access_events")
    op.drop_table("access_events")
    op.drop_table("refused_contacts")
    op.drop_index(op.f("ix_subscriptions_telegram_id"), table_name="subscriptions")
    op.drop_table("subscriptions")
    op.drop_index(op.f("uq_beta_entries_unlinked_username"), table_name="beta_entries")
    op.drop_table("beta_entries")
    ACCESS_EVENT_KIND.drop(op.get_bind(), checkfirst=True)
