"""SQLAlchemy models, one class per table in data-model.md.

Conventions: every table has a UUID ``id``, ``created_at`` and ``updated_at`` (except
``bot_fsm_state``, keyed by the control-bot chat id). Every per-user table carries ``user_id``
with ``ON DELETE CASCADE`` from ``users``. Times are UTC ``timestamptz``. Columns holding
message content, credentials, examples or personal data (names, chat titles; issue #57) use the
encrypted column types from ``scribe.crypto``.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    Text,
    Time,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from scribe.clock import utcnow
from scribe.crypto import (
    OWNER_COLUMN_INFO,
    EncryptedJSON,
    EncryptedJSONBFromPlaintext,
    EncryptedText,
    EncryptedTextFromPlaintext,
    open_owner,
    seal_owner,
)
from scribe.db.base import Base

MAX_DELAY_S = 86400
# Module-level: ``Draft.text`` shadows ``text`` inside that class body.
SERVER_TRUE, SERVER_FALSE = text("true"), text("false")


# --- enums ------------------------------------------------------------------------------------


class UserState(enum.StrEnum):
    setting_up = "setting_up"
    active = "active"
    paused = "paused"
    out_of_office = "out_of_office"


class LLMTier(enum.StrEnum):
    quality = "quality"
    economy = "economy"


class SessionStatus(enum.StrEnum):
    active = "active"
    revoked = "revoked"
    error = "error"


class LoginMethod(enum.StrEnum):
    phone_code = "phone_code"
    qr = "qr"


class ChatKind(enum.StrEnum):
    private = "private"
    group = "group"
    channel_comments = "channel_comments"


class WorkChatOrigin(enum.StrEnum):
    """How a chat became a work chat (specs/008 data-model.md)."""

    picked = "picked"  # chosen by the user (setup picker, /chats ➕)
    auto = "auto"  # picked up by work account mode


class ContactKind(enum.StrEnum):
    person = "person"
    group = "group"


class TurnState(enum.StrEnum):
    waiting = "waiting"
    reading = "reading"
    pending_review = "pending_review"
    answered = "answered"
    rejected = "rejected"
    dropped = "dropped"
    handle_manually = "handle_manually"
    skipped = "skipped"  # 004: read, no draft needed (a resting state)


class DroppedReason(enum.StrEnum):
    manual_reply = "manual_reply"
    paused = "paused"
    chat_disabled = "chat_disabled"
    media_only = "media_only"
    work_account_off = "work_account_off"  # 008: work account mode turned off (auto chats)


class DraftState(enum.StrEnum):
    pending = "pending"
    sent = "sent"
    rejected = "rejected"
    expired = "expired"
    dropped = "dropped"
    superseded = "superseded"


DRAFT_FINAL_STATES = frozenset(DraftState) - {DraftState.pending}


class ProfileScope(enum.StrEnum):
    global_ = "global"
    contact = "contact"


class ProfileState(enum.StrEnum):
    current = "current"
    previous = "previous"
    retired = "retired"


class ProposalState(enum.StrEnum):
    proposed = "proposed"
    accepted = "accepted"
    dismissed = "dismissed"


class CommitmentState(enum.StrEnum):
    open = "open"
    resolved = "resolved"
    deleted = "deleted"


class CommitmentSourceKind(enum.StrEnum):
    """Where a commitment was first found (specs/003-jira-commitments J10)."""

    telegram_message = "telegram_message"
    jira_comment = "jira_comment"


class CommitmentAutoNote(enum.StrEnum):
    """Why Scribe changed a commitment by itself (specs/003-jira-commitments J11)."""

    issue_done = "issue_done"
    source_removed = "source_removed"
    source_changed = "source_changed"
    source_unreachable = "source_unreachable"


class CommitmentClosedBy(enum.StrEnum):
    user = "user"
    system = "system"


class JiraCursorOutcome(enum.StrEnum):
    no_commitment = "no_commitment"
    commitment = "commitment"
    merged = "merged"
    skipped = "skipped"


class TaskProvider(enum.StrEnum):
    trello = "trello"
    jira = "jira"


class ConnectionStatus(enum.StrEnum):
    active = "active"
    error = "error"
    disconnected = "disconnected"


class ActivityKind(enum.StrEnum):
    draft_prepared = "draft_prepared"
    approved = "approved"
    edited_and_approved = "edited_and_approved"
    rejected = "rejected"
    regenerated = "regenerated"
    sent = "sent"
    expired = "expired"
    dropped = "dropped"
    read_now = "read_now"
    profile_refreshed = "profile_refreshed"
    profile_undo = "profile_undo"
    rule_accepted = "rule_accepted"
    data_exported = "data_exported"
    data_deleted = "data_deleted"
    telegram_login = "telegram_login"
    skipped = "skipped"  # 004: meta {category, source, confidence}
    draft_anyway = "draft_anyway"  # 004: meta {skipped_at, category}
    chats_added = "chats_added"  # 007: meta {work_chat_ids}; 008 pick-up: + origin "auto"
    work_account_on = "work_account_on"  # 008
    work_account_off = "work_account_off"  # 008
    chat_excluded = "chat_excluded"  # 008: meta {work_chat_id}
    chat_included = "chat_included"  # 008: meta {work_chat_id}
    # #74: the daily hard cap paused drafting; the notice was sent (one per UTC day). Meta {day}
    budget_paused = "budget_paused"
    # specs/014 auto-send. Meta (ids, counts and codes only): auto_send_on {work_chat_id, source};
    # auto_send_off {work_chat_id | all, reason}; auto_send_offered {work_chat_id, unchanged,
    # total}; auto_send_offer_answered {work_chat_id, answer}; auto_approved {window_s};
    # auto_send_stopped {reason}. The ``sent`` record of an automatic send carries ``auto: true``.
    auto_send_on = "auto_send_on"
    auto_send_off = "auto_send_off"
    auto_send_offered = "auto_send_offered"
    auto_send_offer_answered = "auto_send_offer_answered"
    auto_approved = "auto_approved"
    auto_send_stopped = "auto_send_stopped"


class AutoSendSource(enum.StrEnum):
    """How auto-send was last turned on for a chat (specs/014 FR-001, FR-015)."""

    manual = "manual"  # the user turned it on in /chats
    offer = "offer"  # the user accepted Scribe's offer


class AutoSendOfferState(enum.StrEnum):
    none = "none"
    offered = "offered"
    opted_out = "opted_out"  # "Don't offer for this chat" (FR-017)


class AutoSendStop(enum.StrEnum):
    """Why a draft's countdown ended without a send (specs/014 data-model.md)."""

    user_action = "user_action"
    disabled = "disabled"
    outdated = "outdated"
    ineligible = "ineligible"
    outside_hours = "outside_hours"
    paused = "paused"
    send_failed = "send_failed"


class DraftDecisionKind(enum.StrEnum):
    """What the user (or the countdown) did with a draft (specs/014 research A5)."""

    sent_unchanged = "sent_unchanged"
    sent_edited = "sent_edited"
    rejected = "rejected"
    regenerated = "regenerated"
    auto_sent = "auto_sent"
    auto_cancelled = "auto_cancelled"


class ReplyDecision(enum.StrEnum):
    """Outcome of the reply gate (004 research R2)."""

    draft = "draft"
    skip = "skip"


class SkipCategory(enum.StrEnum):
    thanks = "thanks"
    farewell = "farewell"
    acknowledgment = "acknowledgment"
    forwarded = "forwarded"  # #47: the turn holds only forwarded messages (no model call)
    budget = "budget"  # #74: the user's daily LLM hard cap was reached (no model call)
    informational = "informational"  # 013: the turn only informs and calls for no reaction
    user_rule = "user_rule"  # 013: a user rule decided the skip


class DecisionSource(enum.StrEnum):
    pregate = "pregate"  # not a skip candidate (empty or very long)
    model = "model"  # the classifier decided
    fallback = "fallback"  # classifier failed or was unsure -> draft
    habit = "habit"  # the user usually replies to closings here (FR-005) -> draft
    disabled = "disabled"  # skipping is off for the chat -> draft
    user = "user"  # "draft anyway"
    rule = "rule"  # 013: a user rule decided the draft or the skip


class AccessEventKind(enum.StrEnum):
    beta_added = "beta_added"
    beta_removed = "beta_removed"
    beta_linked = "beta_linked"
    subscription_granted = "subscription_granted"
    subscription_extended = "subscription_extended"
    subscription_ended = "subscription_ended"
    subscription_reminded = "subscription_reminded"
    access_lost = "access_lost"
    access_restored = "access_restored"
    # US5, migration 0006
    application_submitted = "application_submitted"
    application_approved = "application_approved"
    application_rejected = "application_rejected"


class BetaApplicationStatus(enum.StrEnum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


def _pg_enum(enum_cls: type[enum.StrEnum], name: str) -> Enum:
    """Native Postgres enum storing the enum *values* (e.g. ``global``, not ``global_``)."""
    return Enum(
        enum_cls,
        name=name,
        values_callable=lambda cls: [member.value for member in cls],
        validate_strings=True,
    )


# --- mixins -----------------------------------------------------------------------------------


class _Timestamps:
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


def _user_fk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )


# --- tables -----------------------------------------------------------------------------------


class User(_Timestamps, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("delay_min_s <= delay_max_s", name="delay_range"),
        CheckConstraint("delay_min_s >= 0", name="delay_min_non_negative"),
        CheckConstraint(f"delay_max_s <= {MAX_DELAY_S}", name="delay_max_cap"),
        CheckConstraint("work_start < work_end", name="work_hours"),
        CheckConstraint("cardinality(working_days) > 0", name="working_days_non_empty"),
        CheckConstraint("auto_send_window_s BETWEEN 60 AND 900", name="auto_send_window_range"),
        CheckConstraint("language IN ('en', 'ru')", name="language"),
        CheckConstraint(
            "grammatical_gender IS NULL OR grammatical_gender IN ('masculine', 'feminine')",
            name="grammatical_gender",
        ),
        # The scheduler's claim query only looks at users who are not suspended (005).
        Index(
            "ix_users_access_suspended_at_null",
            "access_suspended_at",
            postgresql_where=text("access_suspended_at IS NULL"),
        ),
    )

    telegram_control_chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    # Issue #57: personal data, encrypted (rows from before are read as they are and rewritten
    # by ``retention/reencrypt.py``). Never filtered or sorted in SQL.
    display_name: Mapped[str] = mapped_column(
        EncryptedTextFromPlaintext, nullable=False, default=""
    )
    timezone: Mapped[str] = mapped_column(Text, nullable=False, default="UTC")
    working_days: Mapped[list[int]] = mapped_column(ARRAY(SmallInteger), nullable=False)
    work_start: Mapped[time] = mapped_column(Time, nullable=False)
    work_end: Mapped[time] = mapped_column(Time, nullable=False)
    delay_min_s: Mapped[int] = mapped_column(Integer, nullable=False)
    delay_max_s: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[UserState] = mapped_column(
        _pg_enum(UserState, "user_state"), nullable=False, default=UserState.setting_up
    )
    llm_tier: Mapped[LLMTier] = mapped_column(
        _pg_enum(LLMTier, "llm_tier"),
        nullable=False,
        default=LLMTier.quality,
        server_default=LLMTier.quality.value,
    )
    # Issue #57: text the user wrote, encrypted; NULL still means "not out of office".
    ooo_message: Mapped[str | None] = mapped_column(EncryptedTextFromPlaintext)
    # Control-bot UI language (issue #8; ``scribe.i18n``): "en" or "ru", see migration 0003.
    language: Mapped[str] = mapped_column(Text, nullable=False, default="en", server_default="en")
    # specs/004 FR-S16: search earlier conversations while drafting (toggle in /settings).
    search_earlier_messages: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    setup_step: Mapped[str | None] = mapped_column(Text)
    # 004 FR-014: global switch for skipping closing messages.
    skip_closing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=SERVER_TRUE
    )
    # specs/009 FR-009: mark work chats unread after the agent reads or sends there
    # (toggle in /settings).
    mark_unread: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=SERVER_TRUE
    )
    # Automatic profile refresh switch in /settings; NULL: the user has not chosen, the
    # deployment default applies (``Settings.profile_auto_refresh_default``).
    profile_auto_refresh: Mapped[bool | None] = mapped_column(Boolean)
    # Issue #46: the form drafts use for the user in languages with grammatical gender
    # ("masculine" / "feminine"; NULL: not set, the model follows the examples). Personal data:
    # never logged, exported with the user row, deleted with it.
    grammatical_gender: Mapped[str | None] = mapped_column(Text)
    # Only the user sets it (the one-off question or /settings).
    # When Scribe asked (once; setup or the ``grammatical_gender_prompt`` job), or the user
    # chose a value. NULL: not yet.
    grammatical_gender_prompted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Not asked before this time: a sender's claim (a lease) or the backoff after a retryable
    # send error. NULL: due.
    grammatical_gender_prompt_not_before: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # Retryable send errors so far (the release notes backoff).
    grammatical_gender_prompt_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Set / cleared only by the access_reconcile job (005, research Q6); independent of state.
    access_suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # specs/008: every eligible private chat is a work chat (minus exclusions). Written only by
    # ``scribe.chats.work_account``.
    work_account: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=SERVER_FALSE
    )
    work_account_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # specs/014 FR-007: how long a card with a countdown waits before it is sent (60-900 s).
    auto_send_window_s: Mapped[int] = mapped_column(
        Integer, nullable=False, default=120, server_default="120"
    )
    # specs/014 FR-018: at most one auto-send offer per 24 hours across all chats.
    auto_send_last_offer_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TelegramSession(_Timestamps, Base):
    __tablename__ = "telegram_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    # The stored value is the session string sealed with ``user_id`` (crypto.seal_owner, issue
    # #59): copied into another user's row it is refused. Read and write ``session_string``.
    session_token: Mapped[str] = mapped_column(
        "session_string", EncryptedText, nullable=False, info={OWNER_COLUMN_INFO: "user_id"}
    )
    status: Mapped[SessionStatus] = mapped_column(
        _pg_enum(SessionStatus, "session_status"), nullable=False, default=SessionStatus.active
    )
    # Issue #57: ``str(exc)`` may quote account details, so encrypted; only ``IS NULL`` matters.
    last_error: Mapped[str | None] = mapped_column(EncryptedTextFromPlaintext)
    # Both set on every successful login (setup or reconnect); see migration 0002.
    login_method: Mapped[LoginMethod] = mapped_column(
        _pg_enum(LoginMethod, "telegram_login_method"), nullable=False
    )
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # The connected Telegram account as shown to the user, e.g. "Anna Petrova (@anna_p)" (phone
    # only ever masked); null for sessions stored before migration 0002.
    account_label: Mapped[str | None] = mapped_column(EncryptedText)

    @property
    def session_string(self) -> str:
        return open_owner(str(self.user_id), self.session_token)

    @session_string.setter
    def session_string(self, value: str) -> None:
        if self.user_id is None:
            raise ValueError("set TelegramSession.user_id before session_string")
        self.session_token = seal_owner(str(self.user_id), value)


class WorkChat(_Timestamps, Base):
    __tablename__ = "work_chats"
    __table_args__ = (
        UniqueConstraint("user_id", "telegram_chat_id"),
        CheckConstraint(
            "override_formality IS NULL OR override_formality BETWEEN 1 AND 5",
            name="override_formality_range",
        ),
        CheckConstraint(
            "override_delay_min_s IS NULL OR override_delay_min_s >= 0",
            name="override_delay_min_non_negative",
        ),
        CheckConstraint(
            f"override_delay_max_s IS NULL OR override_delay_max_s <= {MAX_DELAY_S}",
            name="override_delay_max_cap",
        ),
        CheckConstraint(
            "override_delay_min_s IS NULL OR override_delay_max_s IS NULL"
            " OR override_delay_min_s <= override_delay_max_s",
            name="override_delay_range",
        ),
        # specs/008: only a chat picked up by work account mode can be excluded.
        CheckConstraint("origin = 'auto' OR NOT excluded", name="excluded_auto_only"),
        CheckConstraint(
            "unread_mark_floor IS NULL OR unread_mark_floor > 0", name="unread_mark_floor_positive"
        ),
        # specs/014 FR-001: auto-send is on only with a recorded source (manual or offer).
        CheckConstraint(
            "NOT auto_send OR auto_send_source IS NOT NULL", name="auto_send_needs_source"
        ),
    )

    user_id: Mapped[uuid.UUID] = _user_fk()
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Issue #57: encrypted, so never sorted or filtered in SQL (``chats_repo`` sorts in Python).
    title: Mapped[str] = mapped_column(EncryptedTextFromPlaintext, nullable=False, default="")
    kind: Mapped[ChatKind] = mapped_column(_pg_enum(ChatKind, "chat_kind"), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    override_language: Mapped[str | None] = mapped_column(Text)
    override_formality: Mapped[int | None] = mapped_column(SmallInteger)
    override_delay_min_s: Mapped[int | None] = mapped_column(Integer)
    override_delay_max_s: Mapped[int | None] = mapped_column(Integer)
    # 004 FR-014: per-chat override of ``users.skip_closing``; null = use the global value.
    skip_closing: Mapped[bool | None] = mapped_column(Boolean)
    # specs/008 (research R1): ``enabled`` stays the effective flag every reader uses; for an
    # ``auto`` chat it is kept equal to ``users.work_account AND NOT excluded`` by
    # ``scribe.chats.work_account``.
    origin: Mapped[WorkChatOrigin] = mapped_column(
        _pg_enum(WorkChatOrigin, "work_chat_origin"),
        nullable=False,
        default=WorkChatOrigin.picked,
        server_default=WorkChatOrigin.picked.value,
    )
    excluded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=SERVER_FALSE
    )
    # specs/009 FR-005: newest Telegram message id of this chat that the agent marked the chat
    # unread for or the user is known to have seen; null = neither yet. Never decreases.
    unread_mark_floor: Mapped[int | None] = mapped_column(BigInteger)
    # Issue #57: when a message of this chat last reached Scribe (or the row was written),
    # moved at most once a day (``chats_repo.LAST_SEEN_RESOLUTION``). Retention removes only
    # unconfigured rows unseen for 90 days (``retention/purge.py``).
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    # specs/014 FR-001/FR-002: opt-in auto-send for this one chat, off by default. Only
    # ``sending/auto_send.py`` and ``chats/work_account.py`` write it; ``auto_send`` implies
    # ``enabled AND NOT excluded`` (FR-021), kept by every path that disables or excludes a chat.
    auto_send: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=SERVER_FALSE
    )
    auto_send_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    auto_send_source: Mapped[AutoSendSource | None] = mapped_column(
        _pg_enum(AutoSendSource, "auto_send_source")
    )
    # FR-016/FR-017: the offer state and the earliest time of the next offer ("Not now" = +30 d).
    auto_send_offer: Mapped[AutoSendOfferState] = mapped_column(
        _pg_enum(AutoSendOfferState, "auto_send_offer_state"),
        nullable=False,
        default=AutoSendOfferState.none,
        server_default=AutoSendOfferState.none.value,
    )
    auto_send_offer_not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # FR-020: when the "consider turning auto-send off" suggestion was last posted (7 d gap).
    auto_send_warned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Contact(_Timestamps, Base):
    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("user_id", "telegram_peer_id"),)

    user_id: Mapped[uuid.UUID] = _user_fk()
    kind: Mapped[ContactKind] = mapped_column(_pg_enum(ContactKind, "contact_kind"), nullable=False)
    telegram_peer_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Issue #57: encrypted, so never sorted or filtered in SQL.
    display_name: Mapped[str] = mapped_column(
        EncryptedTextFromPlaintext, nullable=False, default=""
    )
    own_message_count_6m: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pinned_profile: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Issue #57: when Scribe last saw this person or group (see ``WorkChat.last_seen_at``).
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


class Message(_Timestamps, Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("work_chat_id", "telegram_message_id"),)

    user_id: Mapped[uuid.UUID] = _user_fk()
    work_chat_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_chats.id", ondelete="CASCADE"), nullable=False
    )
    telegram_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sender_contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL")
    )
    is_outgoing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    text: Mapped[str] = mapped_column(EncryptedText, nullable=False, default="")
    has_media: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    reply_to_telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    # A forwarded copy: its text is not the sender's (or the user's) own words (#47). The origin
    # label (a name or channel title, when known) is message content: encrypted like ``text``
    # and purged with the row.
    is_forwarded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    forwarded_from: Mapped[str | None] = mapped_column(EncryptedText)


class ConversationTurn(_Timestamps, Base):
    __tablename__ = "conversation_turns"
    __table_args__ = (
        CheckConstraint(
            "reply_decision <> 'skip' OR skip_category IS NOT NULL", name="skip_has_category"
        ),
        CheckConstraint(
            "decision_confidence IS NULL OR decision_confidence BETWEEN 0 AND 1",
            name="decision_confidence_range",
        ),
        # /skipped: a user's skipped turns, newest decision first (004 research R5).
        Index(
            "ix_turns_user_skipped",
            "user_id",
            text("decided_at DESC"),
            postgresql_where=text("state = 'skipped'"),
        ),
    )

    user_id: Mapped[uuid.UUID] = _user_fk()
    work_chat_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_chats.id", ondelete="CASCADE"), nullable=False
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    # Nullable: the referenced messages are purged after the retention window.
    first_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL")
    )
    last_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL")
    )
    # Issue #47, groups: the message that opened the turn when ``first_message_id`` was moved
    # back to the sender's forward(s) just before it. Of the sender's messages before this one
    # only the forwards belong to the turn (not their messages to others). Null otherwise.
    opening_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL")
    )
    arrived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delay_s: Mapped[int] = mapped_column(Integer, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    state: Mapped[TurnState] = mapped_column(
        _pg_enum(TurnState, "turn_state"), nullable=False, default=TurnState.waiting
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dropped_reason: Mapped[DroppedReason | None] = mapped_column(
        _pg_enum(DroppedReason, "dropped_reason")
    )
    # Reply gate (004 data-model.md); all null until the gate runs.
    reply_decision: Mapped[ReplyDecision | None] = mapped_column(
        _pg_enum(ReplyDecision, "reply_decision")
    )
    # What the classifier recognised; kept by later decisions (input to the habit rule).
    skip_category: Mapped[SkipCategory | None] = mapped_column(
        _pg_enum(SkipCategory, "skip_category")
    )
    decision_source: Mapped[DecisionSource | None] = mapped_column(
        _pg_enum(DecisionSource, "decision_source")
    )
    decision_confidence: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    draft_anyway_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Issue #47: while the turn holds forwarded messages only, it is not due before this moment
    # (the grouping window after its last forward), so the contact's own message can still
    # join it. Cleared when such a message joins; a replan keeps it (``intake/waiting.py``).
    forwards_hold_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Draft(_Timestamps, Base):
    __tablename__ = "drafts"
    __table_args__ = (
        # Principle I: a draft can only be "sent" after approval: the user's tap
        # (``approved_by_user_at``) or, in a chat the user opted in to, the end of the cancel
        # window they were shown (``auto_approved_at``, specs/014 research A1).
        CheckConstraint(
            "state <> 'sent' OR approved_by_user_at IS NOT NULL OR auto_approved_at IS NOT NULL",
            name="sent_requires_approval",
        ),
        # An automatic approval is only possible for a draft that was shown with a countdown ...
        CheckConstraint(
            "auto_approved_at IS NULL OR auto_send_due_at IS NOT NULL",
            name="auto_approved_needs_window",
        ),
        # ... and a countdown only exists for a card that was posted (research A4a, FR-006).
        CheckConstraint(
            "auto_send_due_at IS NULL OR control_message_id IS NOT NULL",
            name="auto_send_needs_card",
        ),
        # The scheduler's auto-send step reads only running countdowns.
        Index(
            "ix_drafts_auto_send_due",
            "auto_send_due_at",
            postgresql_where=text(
                "state = 'pending' AND auto_send_stop IS NULL AND auto_approved_at IS NULL"
            ),
        ),
        Index(
            "uq_drafts_one_pending_per_turn",
            "conversation_turn_id",
            unique=True,
            postgresql_where=text("state = 'pending'"),
        ),
    )

    user_id: Mapped[uuid.UUID] = _user_fk()
    conversation_turn_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversation_turns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    text: Mapped[str | None] = mapped_column(EncryptedText)
    edited_text: Mapped[str | None] = mapped_column(EncryptedText)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    output_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    style_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("style_profiles.id", ondelete="SET NULL")
    )
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False, default="")
    llm_provider: Mapped[str] = mapped_column(Text, nullable=False, default="")
    llm_model: Mapped[str] = mapped_column(Text, nullable=False, default="")
    llm_usage_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    state: Mapped[DraftState] = mapped_column(
        _pg_enum(DraftState, "draft_state"), nullable=False, default=DraftState.pending
    )
    prepared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    remind_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    approved_by_user_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    # #54: the MTProto random_id of the send, committed before the first attempt and reused on
    # every retry so Telegram never delivers the reply twice.
    send_random_id: Mapped[int | None] = mapped_column(BigInteger)
    outdated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 004 FR-011: a later closing message says the request may be resolved.
    possibly_unnecessary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=SERVER_FALSE
    )
    # Nullable: the card is posted after the draft row exists.
    control_message_id: Mapped[int | None] = mapped_column(BigInteger)
    # specs/014: the end of the cancel window, set together with ``control_message_id`` when the
    # card of an eligible draft is posted (never before, research A4a). NULL: no countdown.
    auto_send_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Why the countdown ended without a send; NULL while it runs or when it never ran.
    auto_send_stop: Mapped[AutoSendStop | None] = mapped_column(
        _pg_enum(AutoSendStop, "auto_send_stop")
    )
    # Set only by the auto-send step when the window ended and the draft was still eligible;
    # ``approved_by_user_at`` keeps meaning "the user tapped Send" (research A1).
    auto_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StyleProfile(_Timestamps, Base):
    __tablename__ = "style_profiles"
    __table_args__ = (
        CheckConstraint(
            "scope = 'global' OR contact_id IS NOT NULL", name="contact_scope_has_contact"
        ),
    )

    user_id: Mapped[uuid.UUID] = _user_fk()
    scope: Mapped[ProfileScope] = mapped_column(
        _pg_enum(ProfileScope, "profile_scope"), nullable=False
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Learned and user-set rules quote the user's phrases (greetings, sign-offs, typical
    # phrases), and the summary describes them: all encrypted, same column types (no migration).
    rules_json: Mapped[dict[str, Any]] = mapped_column(
        EncryptedJSONBFromPlaintext, nullable=False, default=dict
    )
    user_rules_json: Mapped[dict[str, Any]] = mapped_column(
        EncryptedJSONBFromPlaintext, nullable=False, default=dict
    )
    summary_text: Mapped[str] = mapped_column(
        EncryptedTextFromPlaintext, nullable=False, default=""
    )
    examples: Mapped[list[str]] = mapped_column(EncryptedJSON, nullable=False, default=list)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state: Mapped[ProfileState] = mapped_column(
        _pg_enum(ProfileState, "profile_state"), nullable=False, default=ProfileState.current
    )


class StyleRuleProposal(_Timestamps, Base):
    __tablename__ = "style_rule_proposals"

    user_id: Mapped[uuid.UUID] = _user_fk()
    style_profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("style_profiles.id", ondelete="CASCADE"), nullable=False
    )
    rule_json: Mapped[dict[str, Any]] = mapped_column(EncryptedJSONBFromPlaintext, nullable=False)
    evidence_draft_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, default=list
    )
    state: Mapped[ProposalState] = mapped_column(
        _pg_enum(ProposalState, "proposal_state"), nullable=False, default=ProposalState.proposed
    )


class Commitment(_Timestamps, Base):
    """A promise of the user (FR-031), from a Telegram message or a Jira comment (specs/003).

    ``contact_id`` is null for a Jira counterparty who is no Telegram contact; ``to_whom`` then
    names them. ``jira_*`` link the Jira comment the promise was found in (also on a Telegram
    commitment merged with one). ``closed_by`` / ``auto_note`` / ``user_modified`` record who
    changed the row: Scribe's automatic changes never overwrite a row the user changed.
    """

    __tablename__ = "commitments"
    __table_args__ = (
        CheckConstraint("confidence >= 0.7 AND confidence <= 1", name="confidence_range"),
        Index("ix_commitments_user_id_jira_issue_key", "user_id", "jira_issue_key"),
        Index(
            "uq_commitments_open_jira_comment",
            "user_id",
            "jira_comment_id",
            unique=True,
            postgresql_where=text("jira_comment_id IS NOT NULL AND state = 'open'"),
        ),
    )

    user_id: Mapped[uuid.UUID] = _user_fk()
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE")
    )
    to_whom: Mapped[str | None] = mapped_column(EncryptedText)
    source_kind: Mapped[CommitmentSourceKind] = mapped_column(
        _pg_enum(CommitmentSourceKind, "commitment_source_kind"),
        nullable=False,
        default=CommitmentSourceKind.telegram_message,
        server_default=CommitmentSourceKind.telegram_message.value,
    )
    # Plaintext: matched with ``IN`` and indexed (``ix_commitments_user_id_jira_issue_key``);
    # a project key and a number, not personal data.
    jira_issue_key: Mapped[str | None] = mapped_column(Text)
    jira_comment_id: Mapped[str | None] = mapped_column(Text)
    # Issue #57: names the Jira site, encrypted (never filtered in SQL).
    jira_comment_url: Mapped[str | None] = mapped_column(EncryptedTextFromPlaintext)
    restricted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    auto_note: Mapped[CommitmentAutoNote | None] = mapped_column(
        _pg_enum(CommitmentAutoNote, "commitment_auto_note")
    )
    closed_by: Mapped[CommitmentClosedBy | None] = mapped_column(
        _pg_enum(CommitmentClosedBy, "commitment_closed_by")
    )
    user_modified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    what: Mapped[str] = mapped_column(EncryptedTextFromPlaintext, nullable=False)
    due: Mapped[date | None] = mapped_column(Date)
    source_message_text: Mapped[str | None] = mapped_column(EncryptedText)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False)
    state: Mapped[CommitmentState] = mapped_column(
        _pg_enum(CommitmentState, "commitment_state"),
        nullable=False,
        default=CommitmentState.open,
    )


class TaskSourceConnection(_Timestamps, Base):
    __tablename__ = "task_source_connections"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    provider: Mapped[TaskProvider] = mapped_column(
        _pg_enum(TaskProvider, "task_provider"), nullable=False
    )
    credentials: Mapped[dict[str, Any]] = mapped_column(EncryptedJSON, nullable=False)
    # Issue #57: holds the account identity (Jira email / display name) and the site URL, so the
    # whole document is encrypted inside the jsonb (``{"enc": ...}``); never queried in SQL.
    config_json: Mapped[dict[str, Any]] = mapped_column(
        EncryptedJSONBFromPlaintext, nullable=False, default=dict
    )
    status: Mapped[ConnectionStatus] = mapped_column(
        _pg_enum(ConnectionStatus, "connection_status"),
        nullable=False,
        default=ConnectionStatus.active,
    )
    # Jira comment sync (specs/003-jira-commitments J8): when it may run next and the newest
    # issue ``updated`` it has processed.
    next_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sync_watermark: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JiraCommentCursor(_Timestamps, Base):
    """The user's Jira comments already checked for promises (specs/003 J9). No content: the
    body is only fingerprinted (``crypto.comment_hmac``) to notice edits."""

    __tablename__ = "jira_comment_cursor"
    __table_args__ = (UniqueConstraint("user_id", "comment_id"),)

    user_id: Mapped[uuid.UUID] = _user_fk()
    issue_key: Mapped[str] = mapped_column(Text, nullable=False)
    comment_id: Mapped[str] = mapped_column(Text, nullable=False)
    comment_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    body_hmac: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    outcome: Mapped[JiraCursorOutcome] = mapped_column(
        _pg_enum(JiraCursorOutcome, "jira_cursor_outcome"), nullable=False
    )


class DraftDecision(_Timestamps, Base):
    """What the user (or the countdown) did with a draft (specs/014 research A5): the source of
    the per-chat "sent unchanged" rate and of the intervention count. Ids, a code and a flag
    only, never content; deleted with the user and the chat, purged after 31 days
    (``retention/purge.py``), kept (``draft_id`` NULL) when the draft is deleted."""

    __tablename__ = "draft_decisions"
    __table_args__ = (Index("ix_draft_decisions_chat_at", "work_chat_id", "at"),)

    user_id: Mapped[uuid.UUID] = _user_fk()
    work_chat_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_chats.id", ondelete="CASCADE"), nullable=False
    )
    draft_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drafts.id", ondelete="SET NULL")
    )
    decision: Mapped[DraftDecisionKind] = mapped_column(
        _pg_enum(DraftDecisionKind, "draft_decision"), nullable=False
    )
    # The draft had a countdown armed when it was decided.
    during_auto_send: Mapped[bool] = mapped_column(Boolean, nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ActivityRecord(_Timestamps, Base):
    __tablename__ = "activity_records"

    user_id: Mapped[uuid.UUID] = _user_fk()
    kind: Mapped[ActivityKind] = mapped_column(
        _pg_enum(ActivityKind, "activity_kind"), nullable=False
    )
    draft_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drafts.id", ondelete="SET NULL")
    )
    conversation_turn_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversation_turns.id", ondelete="SET NULL")
    )
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    # Metadata only (ids, counts, flags) - never message content.
    meta_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class BotFsmState(Base):
    """aiogram FSM state per control-bot chat (research R2).

    Not keyed by ``user_id`` because it exists before the user row during ``/start``; the
    ``users_delete_bot_fsm_state`` trigger (see migration 0001) removes it with the user.
    """

    __tablename__ = "bot_fsm_state"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    state: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(EncryptedJSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ScheduledJob(_Timestamps, Base):
    __tablename__ = "scheduled_jobs"

    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(Text)


# --- access gate (specs/005-beta-subscription-access) -----------------------------------------
# Keyed by the person's Telegram id (= users.telegram_control_chat_id, research Q1), with no FK
# to users: the person may never have had an account. Never holds message content (FR-007).


class BetaEntry(Base):
    """One invited person, added by Telegram id or by username (linked on first message)."""

    __tablename__ = "beta_entries"
    __table_args__ = (
        CheckConstraint("telegram_id IS NOT NULL OR username IS NOT NULL", name="id_or_username"),
        Index(
            "uq_beta_entries_unlinked_username",
            "username",
            unique=True,
            postgresql_where=text("telegram_id IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    username: Mapped[str | None] = mapped_column(Text)  # lowercase, without "@"
    note: Mapped[str | None] = mapped_column(Text)  # <= 200 chars, operator's note only
    added_by: Mapped[str] = mapped_column(Text, nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Subscription(_Timestamps, Base):
    """A paid access period; active while not cancelled and ``starts_at <= now < ends_at``."""

    __tablename__ = "subscriptions"
    __table_args__ = (CheckConstraint("ends_at > starts_at", name="period"),)

    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    granted_by: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)  # <= 200 chars, e.g. a payment reference
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # FR-016


class RefusedContact(Base):
    """Rate limit and demand count for refused senders (research Q5); no content or names."""

    __tablename__ = "refused_contacts"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class AccessEvent(Base):
    """Audit of access changes (FR-019, research Q10)."""

    __tablename__ = "access_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    kind: Mapped[AccessEventKind] = mapped_column(
        _pg_enum(AccessEventKind, "access_event_kind"), nullable=False
    )
    actor: Mapped[str] = mapped_column(Text, nullable=False)  # "operator" or "system"
    reason: Mapped[str | None] = mapped_column(Text)  # <= 200 chars, operator note only
    # Ids and ISO dates only - never message content.
    meta_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class BetaApplication(_Timestamps, Base):
    """A beta application sent from the bot (US5, research Q12, Q13). The only access row
    holding text the person wrote, and only after they pressed **Send application** (FR-007,
    FR-023); deleted 30 days after the decision (FR-029)."""

    __tablename__ = "beta_applications"
    __table_args__ = (
        Index(
            "uq_beta_applications_pending",
            "telegram_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    # Issue #57: both encrypted (never filtered in SQL; ``beta_entries.username``, used to link a
    # person, stays plaintext). The 1000-character limit is checked by the bot (migration 0017
    # dropped the CHECK, which would measure the ciphertext).
    username: Mapped[str | None] = mapped_column(EncryptedTextFromPlaintext)  # lowercase, no "@"
    use_case: Mapped[str | None] = mapped_column(EncryptedTextFromPlaintext)
    language: Mapped[str] = mapped_column(Text, nullable=False)  # "en" or "ru"
    consent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[BetaApplicationStatus] = mapped_column(
        _pg_enum(BetaApplicationStatus, "beta_application_status"), nullable=False
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[int | None] = mapped_column(BigInteger)  # operator id; NULL from the CLI
    # [[chat_id, message_id], ...] of the copies sent to operators.
    operator_messages: Mapped[list[list[int]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )


# --- release notes (specs/011-release-notes) --------------------------------------------------
# Announcements hold only operator-written text and counts. Deliveries hold no message text and
# go with the user (ON DELETE CASCADE, FR-024).


class ReleaseTrigger(enum.StrEnum):
    auto = "auto"
    manual = "manual"


class ReleaseDeliveryState(enum.StrEnum):
    pending = "pending"
    sending = "sending"
    sent = "sent"
    blocked = "blocked"
    failed = "failed"
    skipped = "skipped"
    unknown = "unknown"


RELEASE_RECEIVED_STATES = frozenset({ReleaseDeliveryState.sent, ReleaseDeliveryState.unknown})
RELEASE_OPEN_STATES = frozenset({ReleaseDeliveryState.pending, ReleaseDeliveryState.sending})


class ReleaseAnnouncement(Base):
    """One decision to send notes: automatic at startup, or manual from the CLI."""

    __tablename__ = "release_announcements"
    __table_args__ = (
        Index(
            "uq_release_announcements_auto_version",
            "release_version",
            unique=True,
            postgresql_where=text("trigger = 'auto'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    release_version: Mapped[str] = mapped_column(Text, nullable=False)  # X.Y.Z
    versions: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)  # covered, ascending
    # {"0.2.0": {"en": "...", "ru": "..."}}: a snapshot of the covered versions' texts.
    notes_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    trigger: Mapped[ReleaseTrigger] = mapped_column(
        _pg_enum(ReleaseTrigger, "release_trigger"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recipients: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class ReleaseDelivery(_Timestamps, Base):
    """One recipient of one announcement (data-model.md state machine)."""

    __tablename__ = "release_deliveries"
    __table_args__ = (
        UniqueConstraint("announcement_id", "user_id"),
        Index(
            "ix_release_deliveries_due",
            "next_attempt_at",
            postgresql_where=text("state = 'pending'"),
        ),
        Index("ix_release_deliveries_user", "user_id"),
    )

    announcement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("release_announcements.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[ReleaseDeliveryState] = mapped_column(
        _pg_enum(ReleaseDeliveryState, "release_delivery_state"),
        nullable=False,
        default=ReleaseDeliveryState.pending,
        server_default=ReleaseDeliveryState.pending.value,
    )
    parts_total: Mapped[int | None] = mapped_column(SmallInteger)  # set when claimed
    parts_sent: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0"
    )
    attempts: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0"
    )
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    skip_reason: Mapped[str | None] = mapped_column(Text)  # no_access, no_account, ...
    error: Mapped[str | None] = mapped_column(Text)  # exception class name only


class LLMSpend(_Timestamps, Base):
    """Daily LLM spend of one user on one route (issue #67): one row per (user, UTC day, route),
    upserted after every billed call (``scribe.db.repos.llm_spend``). Counts and money only,
    never content; deleted with the user (cascade), purged after ``llm_spend_retention_days`` by
    the retention job, and not part of the ``/my_data`` export (operator accounting)."""

    __tablename__ = "llm_spend"
    __table_args__ = (
        UniqueConstraint("user_id", "day", "route"),
        Index("ix_llm_spend_day", "day"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    day: Mapped[date] = mapped_column(Date, nullable=False)  # UTC date of the call's start
    route: Mapped[str] = mapped_column(Text, nullable=False)  # llm route, e.g. compose.draft
    calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # Failed calls the provider still billed (truncated or invalid output); included in calls.
    failed_calls: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    input_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    output_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    reasoning_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    cached_input_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    cache_write_input_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    cache_write_1h_input_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(14, 6), nullable=False, default=Decimal(0), server_default="0"
    )
