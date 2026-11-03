"""Release notes for users after an update (specs/011-release-notes data-model.md).

Revision ID: 0011
Revises: 0012
Create Date: 2026-10-23

Adds ``release_announcements`` (one row per automatic or manual announcement: the versions it
covers and a snapshot of their notes) and ``release_deliveries`` (one row per recipient, keyed
to ``users`` with ``ON DELETE CASCADE``), with the enums ``release_trigger`` and
``release_delivery_state``. Nothing existing changes and no rows are written.

Parallel feature branches also start from 0008 (0009, 0010, 0012, 0013). Whichever merges
later re-chains its ``down_revision`` so that ``alembic heads`` shows exactly one head
(research R12).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RELEASE_TRIGGER = postgresql.ENUM("auto", "manual", name="release_trigger", create_type=False)
RELEASE_DELIVERY_STATE = postgresql.ENUM(
    "pending",
    "sending",
    "sent",
    "blocked",
    "failed",
    "skipped",
    "unknown",
    name="release_delivery_state",
    create_type=False,
)


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    RELEASE_TRIGGER.create(bind, checkfirst=True)
    RELEASE_DELIVERY_STATE.create(bind, checkfirst=True)

    op.create_table(
        "release_announcements",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("release_version", sa.Text(), nullable=False),
        sa.Column("versions", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("notes_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("trigger", RELEASE_TRIGGER, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recipients", sa.Integer(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_release_announcements")),
    )
    op.create_index(
        "uq_release_announcements_auto_version",
        "release_announcements",
        ["release_version"],
        unique=True,
        postgresql_where=sa.text("trigger = 'auto'"),
    )

    op.create_table(
        "release_deliveries",
        sa.Column("announcement_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("state", RELEASE_DELIVERY_STATE, server_default="pending", nullable=False),
        sa.Column("parts_total", sa.SmallInteger(), nullable=True),
        sa.Column("parts_sent", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("attempts", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["announcement_id"],
            ["release_announcements.id"],
            name=op.f("fk_release_deliveries_announcement_id_release_announcements"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_release_deliveries_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_release_deliveries")),
        sa.UniqueConstraint(
            "announcement_id",
            "user_id",
            name=op.f("uq_release_deliveries_announcement_id_user_id"),
        ),
    )
    op.create_index(
        "ix_release_deliveries_due",
        "release_deliveries",
        ["next_attempt_at"],
        postgresql_where=sa.text("state = 'pending'"),
    )
    op.create_index("ix_release_deliveries_user", "release_deliveries", ["user_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_release_deliveries_user", table_name="release_deliveries")
    op.drop_index(
        "ix_release_deliveries_due",
        table_name="release_deliveries",
        postgresql_where=sa.text("state = 'pending'"),
    )
    op.drop_table("release_deliveries")
    op.drop_index(
        "uq_release_announcements_auto_version",
        table_name="release_announcements",
        postgresql_where=sa.text("trigger = 'auto'"),
    )
    op.drop_table("release_announcements")
    bind = op.get_bind()
    RELEASE_DELIVERY_STATE.drop(bind, checkfirst=True)
    RELEASE_TRIGGER.drop(bind, checkfirst=True)
