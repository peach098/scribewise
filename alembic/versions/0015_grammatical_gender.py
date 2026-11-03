"""The user's grammatical gender for drafts (issue #46): ``users.grammatical_gender``
(``masculine`` / ``feminine`` / NULL; only the user sets it),
``users.grammatical_gender_prompted_at`` (when Scribe asked, or the user chose),
``users.grammatical_gender_prompt_not_before`` (a sender's lease, or the backoff after a
retryable send error) and
``users.grammatical_gender_prompt_attempts`` (retryable send errors so far).

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-30

Existing users get NULL (and 0 attempts): drafts behave as before until a value is set, and the
``grammatical_gender_prompt`` job asks each of them once. No rows are written here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("users", sa.Column("grammatical_gender", sa.Text(), nullable=True))
    op.add_column(
        "users",
        sa.Column("grammatical_gender_prompted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column(
            "grammatical_gender_prompt_not_before", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "grammatical_gender_prompt_attempts",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_users_grammatical_gender"),
        "users",
        "grammatical_gender IS NULL OR grammatical_gender IN ('masculine', 'feminine')",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f("ck_users_grammatical_gender"), "users", type_="check")
    op.drop_column("users", "grammatical_gender_prompt_attempts")
    op.drop_column("users", "grammatical_gender_prompt_not_before")
    op.drop_column("users", "grammatical_gender_prompted_at")
    op.drop_column("users", "grammatical_gender")
