"""Jira task source and commitments from Jira comments (specs/003-jira-commitments data-model.md).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-21

``commitments`` gains the Jira link, the counterparty name (a Jira person may be no Telegram
contact, so ``contact_id`` becomes nullable) and who changed the row; rows closed before this
revision were all closed by the user (``/commitments``), so ``closed_by`` is backfilled with
``user``. ``task_source_connections`` gains the comment-sync times, and ``jira_comment_cursor``
remembers which of the user's Jira comments were checked (no content). The downgrade deletes the
commitments that have no contact (only Jira can create them) before restoring ``NOT NULL``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOURCE_KIND = postgresql.ENUM(
    "telegram_message", "jira_comment", name="commitment_source_kind", create_type=False
)
AUTO_NOTE = postgresql.ENUM(
    "issue_done",
    "source_removed",
    "source_changed",
    "source_unreachable",
    name="commitment_auto_note",
    create_type=False,
)
CLOSED_BY = postgresql.ENUM("user", "system", name="commitment_closed_by", create_type=False)
CURSOR_OUTCOME = postgresql.ENUM(
    "no_commitment",
    "commitment",
    "merged",
    "skipped",
    name="jira_cursor_outcome",
    create_type=False,
)
ENUMS = (SOURCE_KIND, AUTO_NOTE, CLOSED_BY, CURSOR_OUTCOME)


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    for enum in ENUMS:
        enum.create(bind, checkfirst=True)

    op.alter_column("commitments", "contact_id", nullable=True)
    op.add_column("commitments", sa.Column("to_whom", sa.Text(), nullable=True))
    op.add_column(
        "commitments",
        sa.Column("source_kind", SOURCE_KIND, nullable=False, server_default="telegram_message"),
    )
    op.add_column("commitments", sa.Column("jira_issue_key", sa.Text(), nullable=True))
    op.add_column("commitments", sa.Column("jira_comment_id", sa.Text(), nullable=True))
    op.add_column("commitments", sa.Column("jira_comment_url", sa.Text(), nullable=True))
    op.add_column(
        "commitments",
        sa.Column("restricted", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("commitments", sa.Column("auto_note", AUTO_NOTE, nullable=True))
    op.add_column("commitments", sa.Column("closed_by", CLOSED_BY, nullable=True))
    op.add_column(
        "commitments",
        sa.Column("user_modified", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute("UPDATE commitments SET closed_by = 'user' WHERE closed_at IS NOT NULL")
    op.create_index(
        "ix_commitments_user_id_jira_issue_key",
        "commitments",
        ["user_id", "jira_issue_key"],
        unique=False,
    )
    op.create_index(
        "uq_commitments_open_jira_comment",
        "commitments",
        ["user_id", "jira_comment_id"],
        unique=True,
        postgresql_where=sa.text("jira_comment_id IS NOT NULL AND state = 'open'"),
    )

    op.add_column(
        "task_source_connections",
        sa.Column("next_sync_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "task_source_connections",
        sa.Column("sync_watermark", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "jira_comment_cursor",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("issue_key", sa.Text(), nullable=False),
        sa.Column("comment_id", sa.Text(), nullable=False),
        sa.Column("comment_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("body_hmac", sa.LargeBinary(), nullable=False),
        sa.Column("outcome", CURSOR_OUTCOME, nullable=False),
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
            ["user_id"],
            ["users.id"],
            name=op.f("fk_jira_comment_cursor_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jira_comment_cursor")),
        sa.UniqueConstraint(
            "user_id", "comment_id", name=op.f("uq_jira_comment_cursor_user_id_comment_id")
        ),
    )
    op.create_index(
        op.f("ix_jira_comment_cursor_user_id"), "jira_comment_cursor", ["user_id"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_jira_comment_cursor_user_id"), table_name="jira_comment_cursor")
    op.drop_table("jira_comment_cursor")
    op.drop_column("task_source_connections", "sync_watermark")
    op.drop_column("task_source_connections", "next_sync_at")

    op.drop_index("uq_commitments_open_jira_comment", table_name="commitments")
    op.drop_index("ix_commitments_user_id_jira_issue_key", table_name="commitments")
    for column in (
        "user_modified",
        "closed_by",
        "auto_note",
        "restricted",
        "jira_comment_url",
        "jira_comment_id",
        "jira_issue_key",
        "source_kind",
        "to_whom",
    ):
        op.drop_column("commitments", column)
    op.execute("DELETE FROM commitments WHERE contact_id IS NULL")
    op.alter_column("commitments", "contact_id", nullable=False)

    bind = op.get_bind()
    for enum in reversed(ENUMS):
        enum.drop(bind, checkfirst=True)
