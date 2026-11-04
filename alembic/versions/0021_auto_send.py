"""Auto-send for trusted chats (specs/014-auto-send data-model.md): per-chat opt-in and offer
state on ``work_chats``, the cancel window on ``users``, the countdown and its automatic
approval on ``drafts``, the ``draft_decisions`` table and the new ``activity_kind`` values.

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-04

Everything is off or empty for existing rows: ``work_chats.auto_send = false``,
``auto_send_offer = 'none'``, ``users.auto_send_window_s = 120``, no countdown on any draft.

Principle I stays structural: ``sent_requires_approval`` is replaced by "a sent draft needs
``approved_by_user_at`` (the user's tap) or ``auto_approved_at`` (the end of a cancel window the
user was shown)"; ``auto_approved_at`` additionally needs ``auto_send_due_at``, and a countdown
(``auto_send_due_at``) needs a posted card (``control_message_id``), so a draft the user has not
seen cannot be approved automatically.

The ``activity_kind`` values are added first, in autocommit mode, as in 0013 and 0019: Postgres
refuses to use an enum value in the transaction that added it.

The downgrade drops the table, columns, index, checks and the four new types. Postgres has no
``ALTER TYPE ... DROP VALUE``, so the six ``activity_kind`` values stay in their type and the
activity rows using them are deleted (older code could not load them). A draft that was sent
automatically keeps its place as a sent draft: its ``auto_approved_at`` is copied into
``approved_by_user_at`` so the restored check holds (the distinction is lost on downgrade).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0021"
down_revision: str | Sequence[str] | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_ACTIVITY_KINDS = (
    "auto_send_on",
    "auto_send_off",
    "auto_send_offered",
    "auto_send_offer_answered",
    "auto_approved",
    "auto_send_stopped",
)
ENUMS: dict[str, tuple[str, ...]] = {
    "auto_send_source": ("manual", "offer"),
    "auto_send_offer_state": ("none", "offered", "opted_out"),
    "auto_send_stop": (
        "user_action",
        "disabled",
        "outdated",
        "ineligible",
        "outside_hours",
        "paused",
        "send_failed",
    ),
    "draft_decision": (
        "sent_unchanged",
        "sent_edited",
        "rejected",
        "regenerated",
        "auto_sent",
        "auto_cancelled",
    ),
}


def _enum(name: str) -> postgresql.ENUM:
    """The type as a column type; it is created and dropped explicitly below."""
    return postgresql.ENUM(*ENUMS[name], name=name, create_type=False)


def upgrade() -> None:
    """Upgrade schema."""
    with op.get_context().autocommit_block():
        for value in NEW_ACTIVITY_KINDS:
            op.execute(f"ALTER TYPE activity_kind ADD VALUE IF NOT EXISTS '{value}'")

    for name in ENUMS:
        _enum(name).create(op.get_bind(), checkfirst=True)

    op.add_column(
        "work_chats",
        sa.Column("auto_send", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "work_chats", sa.Column("auto_send_changed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("work_chats", sa.Column("auto_send_source", _enum("auto_send_source")))
    op.add_column(
        "work_chats",
        sa.Column(
            "auto_send_offer", _enum("auto_send_offer_state"), server_default="none", nullable=False
        ),
    )
    op.add_column(
        "work_chats",
        sa.Column("auto_send_offer_not_before", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "work_chats", sa.Column("auto_send_warned_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_work_chats_auto_send_needs_source"),
        "work_chats",
        "NOT auto_send OR auto_send_source IS NOT NULL",
    )

    op.add_column(
        "users",
        sa.Column("auto_send_window_s", sa.Integer(), server_default="120", nullable=False),
    )
    op.add_column(
        "users", sa.Column("auto_send_last_offer_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_users_auto_send_window_range"),
        "users",
        "auto_send_window_s BETWEEN 60 AND 900",
    )

    op.add_column(
        "drafts", sa.Column("auto_send_due_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("drafts", sa.Column("auto_send_stop", _enum("auto_send_stop")))
    op.add_column(
        "drafts", sa.Column("auto_approved_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.drop_constraint(op.f("ck_drafts_sent_requires_approval"), "drafts", type_="check")
    op.create_check_constraint(
        op.f("ck_drafts_sent_requires_approval"),
        "drafts",
        "state <> 'sent' OR approved_by_user_at IS NOT NULL OR auto_approved_at IS NOT NULL",
    )
    op.create_check_constraint(
        op.f("ck_drafts_auto_approved_needs_window"),
        "drafts",
        "auto_approved_at IS NULL OR auto_send_due_at IS NOT NULL",
    )
    op.create_check_constraint(
        op.f("ck_drafts_auto_send_needs_card"),
        "drafts",
        "auto_send_due_at IS NULL OR control_message_id IS NOT NULL",
    )
    op.create_index(
        "ix_drafts_auto_send_due",
        "drafts",
        ["auto_send_due_at"],
        postgresql_where=sa.text(
            "state = 'pending' AND auto_send_stop IS NULL AND auto_approved_at IS NULL"
        ),
    )

    op.create_table(
        "draft_decisions",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("work_chat_id", sa.UUID(), nullable=False),
        sa.Column("draft_id", sa.UUID(), nullable=True),
        sa.Column("decision", _enum("draft_decision"), nullable=False),
        sa.Column("during_auto_send", sa.Boolean(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
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
            ["draft_id"],
            ["drafts.id"],
            name=op.f("fk_draft_decisions_draft_id_drafts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_draft_decisions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["work_chat_id"],
            ["work_chats.id"],
            name=op.f("fk_draft_decisions_work_chat_id_work_chats"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_draft_decisions")),
    )
    op.create_index(op.f("ix_draft_decisions_user_id"), "draft_decisions", ["user_id"])
    op.create_index("ix_draft_decisions_chat_at", "draft_decisions", ["work_chat_id", "at"])


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        "DELETE FROM activity_records WHERE kind IN ("
        + ", ".join(f"'{value}'" for value in NEW_ACTIVITY_KINDS)
        + ")"
    )
    op.drop_index("ix_draft_decisions_chat_at", table_name="draft_decisions")
    op.drop_index(op.f("ix_draft_decisions_user_id"), table_name="draft_decisions")
    op.drop_table("draft_decisions")

    op.drop_index("ix_drafts_auto_send_due", table_name="drafts")
    op.drop_constraint(op.f("ck_drafts_auto_send_needs_card"), "drafts", type_="check")
    op.drop_constraint(op.f("ck_drafts_auto_approved_needs_window"), "drafts", type_="check")
    op.drop_constraint(op.f("ck_drafts_sent_requires_approval"), "drafts", type_="check")
    # Keep the sent drafts valid under the old check (see the module docstring).
    op.execute(
        "UPDATE drafts SET approved_by_user_at = auto_approved_at"
        " WHERE state = 'sent' AND approved_by_user_at IS NULL"
    )
    op.create_check_constraint(
        op.f("ck_drafts_sent_requires_approval"),
        "drafts",
        "state <> 'sent' OR approved_by_user_at IS NOT NULL",
    )
    op.drop_column("drafts", "auto_approved_at")
    op.drop_column("drafts", "auto_send_stop")
    op.drop_column("drafts", "auto_send_due_at")

    op.drop_constraint(op.f("ck_users_auto_send_window_range"), "users", type_="check")
    op.drop_column("users", "auto_send_last_offer_at")
    op.drop_column("users", "auto_send_window_s")

    op.drop_constraint(op.f("ck_work_chats_auto_send_needs_source"), "work_chats", type_="check")
    op.drop_column("work_chats", "auto_send_warned_at")
    op.drop_column("work_chats", "auto_send_offer_not_before")
    op.drop_column("work_chats", "auto_send_offer")
    op.drop_column("work_chats", "auto_send_source")
    op.drop_column("work_chats", "auto_send_changed_at")
    op.drop_column("work_chats", "auto_send")

    for name in ENUMS:
        _enum(name).drop(op.get_bind(), checkfirst=True)
    # The activity_kind values stay: Postgres has no ALTER TYPE ... DROP VALUE. The upgrade adds
    # them with IF NOT EXISTS, so upgrading again is safe.
