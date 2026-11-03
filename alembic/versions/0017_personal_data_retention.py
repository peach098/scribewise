"""Personal data at rest (issue #57): ``work_chats.last_seen_at`` and ``contacts.last_seen_at``,
and no database CHECK on the length of ``beta_applications.use_case``.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-31

- ``last_seen_at``: when a message of the chat / from the contact last reached Scribe. The
  retention job removes unconfigured chats and unreferenced contacts unseen for 90 days.
  Existing rows get the time of the upgrade, so nothing is removed for 90 days after it.
- ``use_case`` (like ``username``, ``work_chats.title``, ``contacts.display_name``,
  ``users.display_name``, ``users.ooo_message``, ``telegram_sessions.last_error``,
  ``commitments.jira_comment_url`` and ``task_source_connections.config_json``) is now written
  encrypted with the same database type; a CHECK on its length would measure the ciphertext,
  so the bot alone enforces the 1000 characters. Rows written before are rewritten by the
  ``reencrypt_profiles`` job, not here (this migration writes no rows).

Downgrade drops the columns and restores the CHECK as ``NOT VALID`` (encrypted values are
longer); it does not turn ciphertext back into plaintext. Code older than this release reads
the columns above as plain text (and ``config_json`` as plain JSON): once the new code has
written to them, older code shows ciphertext as chat titles and names and cannot use the
Jira/Trello connections. Roll back to a database backup taken before the upgrade instead.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    for table in ("work_chats", "contacts"):
        op.add_column(
            table,
            sa.Column(
                "last_seen_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
        )
    op.drop_constraint(
        op.f("ck_beta_applications_use_case_length"), "beta_applications", type_="check"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        "ALTER TABLE beta_applications ADD CONSTRAINT ck_beta_applications_use_case_length "
        "CHECK (char_length(use_case) <= 1000) NOT VALID"
    )
    for table in ("contacts", "work_chats"):
        op.drop_column(table, "last_seen_at")
