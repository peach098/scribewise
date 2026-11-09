"""Paying with Telegram Stars (specs/015-telegram-stars-payments data-model.md): the offers,
Stars subscriptions and payments tables, ``subscriptions.source`` / ``payment_id`` and seven new
``access_event_kind`` values.

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-07

Existing ``subscriptions`` rows become ``source = 'operator'`` with no payment. The three new
tables are keyed by the person's Telegram id (no FK to ``users``: people without an account can
pay) and hold ids, amounts and dates only, never content.

The ``access_event_kind`` values are added first, in autocommit mode, as in 0021: Postgres
refuses to use an enum value in the transaction that added it. The downgrade deletes the
``access_events`` rows with the new kinds (older code could not load them), then drops the
columns, tables and the six new types. Postgres has no ``ALTER TYPE ... DROP VALUE``, so the
seven values stay in their type; the upgrade adds them with ``IF NOT EXISTS``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0022"
down_revision: str | Sequence[str] | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_EVENT_KINDS = (
    "payment_received",
    "payment_renewed",
    "payment_refunded",
    "stars_canceled",
    "stars_resumed",
    "stars_failed",
    "stars_ended",
)
ENUMS: dict[str, tuple[str, ...]] = {
    "stars_sub_state": ("active", "canceled", "ended"),
    "stars_cancel_source": ("user_bot", "telegram", "refund", "deletion", "operator"),
    "stars_end_reason": ("canceled", "failed", "refunded", "expired"),
    "payment_kind": ("first", "renewal"),
    "payment_source": ("update", "backfill"),
    "subscription_source": ("operator", "stars"),
}


def _enum(name: str) -> postgresql.ENUM:
    """The type as a column type; it is created and dropped explicitly below."""
    return postgresql.ENUM(*ENUMS[name], name=name, create_type=False)


def _timestamps() -> list[sa.Column[object]]:
    return [
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
    ]


def upgrade() -> None:
    """Upgrade schema."""
    with op.get_context().autocommit_block():
        for value in NEW_EVENT_KINDS:
            op.execute(f"ALTER TYPE access_event_kind ADD VALUE IF NOT EXISTS '{value}'")

    for name in ENUMS:
        _enum(name).create(op.get_bind(), checkfirst=True)

    op.create_table(
        "payment_offers",
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("price_stars", sa.Integer(), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "price_stars BETWEEN 1 AND 10000", name=op.f("ck_payment_offers_price_range")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_offers")),
    )
    op.create_index(op.f("ix_payment_offers_telegram_id"), "payment_offers", ["telegram_id"])

    op.create_table(
        "stars_subscriptions",
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("offer_id", sa.UUID(), nullable=False),
        sa.Column("first_charge_id", sa.Text(), nullable=False),
        sa.Column("price_stars", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", _enum("stars_sub_state"), nullable=False),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_by", _enum("stars_cancel_source"), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_reason", _enum("stars_end_reason"), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["offer_id"],
            ["payment_offers.id"],
            name=op.f("fk_stars_subscriptions_offer_id_payment_offers"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stars_subscriptions")),
        sa.UniqueConstraint("offer_id", name=op.f("uq_stars_subscriptions_offer_id")),
        sa.UniqueConstraint("first_charge_id", name=op.f("uq_stars_subscriptions_first_charge_id")),
    )
    op.create_index(
        op.f("ix_stars_subscriptions_telegram_id"), "stars_subscriptions", ["telegram_id"]
    )
    op.create_index(
        "uq_stars_subscriptions_running",
        "stars_subscriptions",
        ["telegram_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('active','canceled')"),
    )

    op.create_table(
        "payments",
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("charge_id", sa.Text(), nullable=False),
        sa.Column("amount_stars", sa.Integer(), nullable=False),
        sa.Column("kind", _enum("payment_kind"), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stars_subscription_id", sa.UUID(), nullable=True),
        sa.Column("credited_via", _enum("payment_source"), nullable=False),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refunded_by", sa.Text(), nullable=True),
        sa.Column("owner_deleted_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("amount_stars > 0", name=op.f("ck_payments_amount_positive")),
        sa.CheckConstraint("period_end > period_start", name=op.f("ck_payments_period")),
        sa.ForeignKeyConstraint(
            ["stars_subscription_id"],
            ["stars_subscriptions.id"],
            name=op.f("fk_payments_stars_subscription_id_stars_subscriptions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payments")),
        sa.UniqueConstraint("charge_id", name=op.f("uq_payments_charge_id")),
    )
    op.create_index(op.f("ix_payments_telegram_id"), "payments", ["telegram_id"])
    op.create_index(op.f("ix_payments_paid_at"), "payments", ["paid_at"])

    op.add_column(
        "subscriptions",
        sa.Column(
            "source", _enum("subscription_source"), server_default="operator", nullable=False
        ),
    )
    op.add_column("subscriptions", sa.Column("payment_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_subscriptions_payment_id_payments"),
        "subscriptions",
        "payments",
        ["payment_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        op.f("uq_subscriptions_payment_id"), "subscriptions", ["payment_id"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        "DELETE FROM access_events WHERE kind IN ("
        + ", ".join(f"'{value}'" for value in NEW_EVENT_KINDS)
        + ")"
    )
    op.drop_constraint(op.f("uq_subscriptions_payment_id"), "subscriptions", type_="unique")
    op.drop_constraint(
        op.f("fk_subscriptions_payment_id_payments"), "subscriptions", type_="foreignkey"
    )
    op.drop_column("subscriptions", "payment_id")
    op.drop_column("subscriptions", "source")

    op.drop_index(op.f("ix_payments_paid_at"), table_name="payments")
    op.drop_index(op.f("ix_payments_telegram_id"), table_name="payments")
    op.drop_table("payments")
    op.drop_index("uq_stars_subscriptions_running", table_name="stars_subscriptions")
    op.drop_index(op.f("ix_stars_subscriptions_telegram_id"), table_name="stars_subscriptions")
    op.drop_table("stars_subscriptions")
    op.drop_index(op.f("ix_payment_offers_telegram_id"), table_name="payment_offers")
    op.drop_table("payment_offers")

    for name in ENUMS:
        _enum(name).drop(op.get_bind(), checkfirst=True)
    # The access_event_kind values stay: Postgres has no ALTER TYPE ... DROP VALUE. The upgrade
    # adds them with IF NOT EXISTS, so upgrading again is safe.
