"""Retention (FR-020, research R10): a daily job that removes content past its window.

- ``messages`` with ``sent_at`` older than ``message_retention_days``;
- draft content 30 days after the draft reached a final state: ``text``, ``edited_text`` and
  the content keys of ``output_json`` (the encrypted ``enc`` blob, and the plaintext keys of
  rows written before it, see ``drafting/stored_output.py``); the metadata keys stay;
- ``commitments`` 30 days after they were resolved or deleted, and the quoted source
  (``source_message_text``) of every commitment, open ones included, once the message it quotes
  is older than ``message_retention_days`` (``what`` / ``to_whom`` / ``due`` stay; issue #57);
- ``style_profiles`` retired more than 30 days ago (kept that long for undo), and ``previous``
  versions (the undo target, up to 50 encrypted examples) more than 30 days after they were
  replaced, unless the user's latest undoable refresh would restore them;
- ``refused_contacts`` not seen for 30 days (by ``last_seen_at``), and ``access_events`` older
  than 365 days (specs/005-beta-subscription-access FR-007, research Q5 and Q10);
- decided ``beta_applications`` 30 days after the decision (pending ones stay until decided),
  and abandoned beta application flows: ``bot_fsm_state`` rows in ``ApplyStates`` (holding an
  unsent use case) untouched for 24 h (specs/005 FR-007, FR-029, research Q12).
- Payments (specs/015 data-model.md "Purge"): ``payments`` 1096 days after ``owner_deleted_at``
  (the minimal record kept after ``/delete_my_data``; rows of existing accounts stay),
  ``stars_subscriptions`` that ended and have no payment left, and ``payment_offers`` older than
  7 days that no subscription references.
- ``llm_spend`` rows (issue #67) whose UTC ``day`` is older than ``llm_spend_retention_days``
  (90 by default).
- ``jira_comment_cursor`` rows 30 days after the comment was last changed (specs/003 J13): such
  comments are outside the sync's lookback window anyway.
- Stale chat and contact metadata (issue #57), unseen (``last_seen_at``) for 90 days:

  - a ``work_chats`` row only when it is no decision of the user: an ``auto`` chat paused
    because work account mode is off (not excluded, not enabled, the user's mode off), with no
    per-chat setting (overrides, ``skip_closing``), no stored message, no turn that is not
    finished or arrived within the 90 days, and no draft pending or decided within them. Its
    finished turns and their drafts (content already removed above) go with it. Picked chats
    (enabled or disabled), excluded chats and chats with settings are never removed: the row
    *is* the user's choice, and a removed disabled chat would be picked up again by the mode.
    Turning the mode back on picks such a chat up again on its next message (specs/008 R2);
  - a ``contacts`` row only when nothing references it: no work chat of the same Telegram id
    (the chat's own contact), no turn, message, commitment or style profile, and no pinned
    profile. Mostly group members and counterparties of closed commitments.

Abandoned Trello/Jira connect flows (pending credentials in ``bot_fsm_state``) need a shorter
bound than a daily run: ``retention/pending_credentials.py`` purges them every minute (issue #56).

Account closure is a plain ``DELETE FROM users`` (cascade), see ``db.repos.users``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy import Text, and_, cast, delete, exists, or_, select, update
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, array
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.access.apply import ApplyStates
from scribe.clock import Clock, utcnow
from scribe.config import Settings
from scribe.db.models import (
    AccessEvent,
    ActivityRecord,
    BetaApplication,
    BotFsmState,
    Commitment,
    Contact,
    ConversationTurn,
    Draft,
    DraftState,
    JiraCommentCursor,
    LLMSpend,
    Message,
    Payment,
    PaymentOffer,
    ProfileState,
    RefusedContact,
    StarsSubscription,
    StarsSubState,
    StyleProfile,
    TurnState,
    User,
    WorkChat,
    WorkChatOrigin,
)
from scribe.db.repos import decisions as decisions_repo
from scribe.drafting.stored_output import CONTENT_KEYS
from scribe.logging import get_logger
from scribe.retention.user_data import DECISION_RETENTION
from scribe.scheduling.scheduler import Daily, Scheduler
from scribe.style.profiles import last_undoable_refresh

log = get_logger("scribe.retention")

JOB_NAME = "purge_messages"
JOB_SCHEDULE = Daily(at=time(3, 0))  # UTC, low-traffic hour
DERIVED_RETENTION = timedelta(days=30)
REFUSED_CONTACT_RETENTION = timedelta(days=30)
ACCESS_EVENT_RETENTION = timedelta(days=365)
PAYMENT_RETENTION = timedelta(days=1096)  # 3 years after owner_deleted_at (specs/015 FR-020)
OFFER_RETENTION = timedelta(days=7)  # an unused offer and its invoice link (specs/015 research P2)
APPLICATION_RETENTION = timedelta(days=30)  # after the decision
APPLY_FLOW_RETENTION = timedelta(hours=24)  # an unsent use case in the FSM
APPLY_STATE_PREFIX = f"{ApplyStates.__name__}:"  # aiogram stores "ApplyStates:use_case"
STALE_RETENTION = timedelta(days=90)  # chats and contacts unseen this long (issue #57)
# A turn in one of these states still waits for Scribe or the user.
OPEN_TURN_STATES = (
    TurnState.waiting,
    TurnState.reading,
    TurnState.pending_review,
    TurnState.handle_manually,
)


@dataclass(frozen=True)
class PurgeStats:
    messages_deleted: int
    drafts_redacted: int
    commitments_deleted: int
    profiles_deleted: int
    refused_contacts_deleted: int
    access_events_deleted: int
    jira_cursors_deleted: int = 0
    applications_deleted: int = 0
    apply_flows_deleted: int = 0
    commitment_sources_cleared: int = 0
    chats_deleted: int = 0
    contacts_deleted: int = 0
    llm_spend_deleted: int = 0
    decisions_deleted: int = 0
    payments_deleted: int = 0
    stars_subscriptions_deleted: int = 0
    offers_deleted: int = 0


def _rowcount(result: object) -> int:
    return int(result.rowcount) if isinstance(result, CursorResult) else 0


async def purge_messages(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings, now: datetime
) -> PurgeStats:
    message_cutoff = now - timedelta(days=settings.message_retention_days)
    derived_cutoff = now - DERIVED_RETENTION
    async with session_factory() as session, session.begin():
        messages = await session.execute(delete(Message).where(Message.sent_at < message_cutoff))
        content_keys = cast(array(list(CONTENT_KEYS)), ARRAY(Text))
        drafts = await session.execute(
            update(Draft)
            .where(
                Draft.finalized_at < derived_cutoff,
                or_(
                    Draft.text.is_not(None),
                    Draft.edited_text.is_not(None),
                    Draft.output_json.has_any(content_keys),
                ),
            )
            .values(
                text=None,
                edited_text=None,
                output_json=Draft.output_json.op("-", return_type=JSONB)(content_keys),
            )
        )
        commitments = await session.execute(
            delete(Commitment).where(Commitment.closed_at < derived_cutoff)
        )
        sources = await session.execute(
            update(Commitment)
            .where(
                Commitment.source_sent_at < message_cutoff,
                Commitment.source_message_text.is_not(None),
            )
            .values(source_message_text=None)
        )
        profiles = await session.execute(
            delete(StyleProfile).where(
                StyleProfile.state == ProfileState.retired,
                StyleProfile.updated_at < derived_cutoff,
            )
        )
        previous = await _purge_previous_profiles(session, derived_cutoff)
        refused = await session.execute(
            delete(RefusedContact).where(
                RefusedContact.last_seen_at < now - REFUSED_CONTACT_RETENTION
            )
        )
        events = await session.execute(
            delete(AccessEvent).where(AccessEvent.at < now - ACCESS_EVENT_RETENTION)
        )
        cursors = await session.execute(
            delete(JiraCommentCursor).where(JiraCommentCursor.comment_updated_at < derived_cutoff)
        )
        spend = await session.execute(
            delete(LLMSpend).where(
                LLMSpend.day < (now - timedelta(days=settings.llm_spend_retention_days)).date()
            )
        )
        decisions = await decisions_repo.purge_older_than(session, now - DECISION_RETENTION)
        # Payments (specs/015): the record of a deleted owner, then what has nothing left.
        payments = await session.execute(
            delete(Payment).where(Payment.owner_deleted_at < now - PAYMENT_RETENTION)
        )
        stars = await session.execute(
            delete(StarsSubscription).where(
                StarsSubscription.state == StarsSubState.ended,
                ~exists().where(Payment.stars_subscription_id == StarsSubscription.id),
            )
        )
        offers = await session.execute(
            delete(PaymentOffer).where(
                PaymentOffer.created_at < now - OFFER_RETENTION,
                ~exists().where(StarsSubscription.offer_id == PaymentOffer.id),
            )
        )
        applications = await session.execute(
            delete(BetaApplication).where(BetaApplication.decided_at < now - APPLICATION_RETENTION)
        )
        apply_flows = await session.execute(
            delete(BotFsmState).where(
                BotFsmState.state.startswith(APPLY_STATE_PREFIX, autoescape=True),
                BotFsmState.updated_at < now - APPLY_FLOW_RETENTION,
            )
        )
        # Chats first: the contact of a chat removed here is then unreferenced.
        stale_cutoff = now - STALE_RETENTION
        chats = await session.execute(delete(WorkChat).where(*stale_chat_conditions(stale_cutoff)))
        contacts = await session.execute(
            delete(Contact).where(*stale_contact_conditions(stale_cutoff))
        )
    stats = PurgeStats(
        messages_deleted=_rowcount(messages),
        drafts_redacted=_rowcount(drafts),
        commitments_deleted=_rowcount(commitments),
        profiles_deleted=_rowcount(profiles) + previous,
        refused_contacts_deleted=_rowcount(refused),
        access_events_deleted=_rowcount(events),
        jira_cursors_deleted=_rowcount(cursors),
        applications_deleted=_rowcount(applications),
        apply_flows_deleted=_rowcount(apply_flows),
        commitment_sources_cleared=_rowcount(sources),
        chats_deleted=_rowcount(chats),
        contacts_deleted=_rowcount(contacts),
        llm_spend_deleted=_rowcount(spend),
        decisions_deleted=decisions,
        payments_deleted=_rowcount(payments),
        stars_subscriptions_deleted=_rowcount(stars),
        offers_deleted=_rowcount(offers),
    )
    log.info("retention_purge", **asdict(stats))
    return stats


def stale_chat_conditions(cutoff: datetime) -> list[Any]:
    """The ``work_chats`` rows the purge may delete (see the module docstring)."""
    turn = ConversationTurn
    return [
        WorkChat.last_seen_at < cutoff,
        WorkChat.origin == WorkChatOrigin.auto,
        WorkChat.excluded.is_(False),
        WorkChat.enabled.is_(False),
        WorkChat.override_language.is_(None),
        WorkChat.override_formality.is_(None),
        WorkChat.override_delay_min_s.is_(None),
        WorkChat.override_delay_max_s.is_(None),
        WorkChat.skip_closing.is_(None),
        exists().where(User.id == WorkChat.user_id, User.work_account.is_(False)),
        ~exists().where(Message.work_chat_id == WorkChat.id),
        ~exists().where(
            turn.work_chat_id == WorkChat.id,
            or_(turn.state.in_(OPEN_TURN_STATES), turn.arrived_at >= cutoff),
        ),
        ~exists().where(
            turn.work_chat_id == WorkChat.id,
            Draft.conversation_turn_id == turn.id,
            or_(Draft.state == DraftState.pending, Draft.finalized_at >= cutoff),
        ),
    ]


def stale_contact_conditions(cutoff: datetime) -> list[Any]:
    """The ``contacts`` rows the purge may delete: unseen and referenced by nothing."""
    return [
        Contact.last_seen_at < cutoff,
        Contact.pinned_profile.is_(False),
        ~exists().where(
            and_(
                WorkChat.user_id == Contact.user_id,
                WorkChat.telegram_chat_id == Contact.telegram_peer_id,
            )
        ),
        ~exists().where(ConversationTurn.contact_id == Contact.id),
        ~exists().where(Message.sender_contact_id == Contact.id),
        ~exists().where(Commitment.contact_id == Contact.id),
        ~exists().where(StyleProfile.contact_id == Contact.id),
    ]


async def _purge_previous_profiles(session: AsyncSession, cutoff: datetime) -> int:
    """Delete ``previous`` profile versions replaced before ``cutoff``, except the ones the
    user's latest undoable refresh would restore (its ``replaced`` old ids)."""
    candidates = (
        await session.execute(
            select(StyleProfile.id, StyleProfile.user_id).where(
                StyleProfile.state == ProfileState.previous,
                StyleProfile.updated_at < cutoff,
            )
        )
    ).all()
    keep: set[str] = set()
    for user_id in {user_id for _, user_id in candidates}:
        batch_id = await last_undoable_refresh(session, user_id)
        if batch_id is None:
            continue
        meta = await session.scalar(
            select(ActivityRecord.meta_json).where(
                ActivityRecord.user_id == user_id, ActivityRecord.id == batch_id
            )
        )
        keep.update(str(old) for _, old in (meta or {}).get("replaced", []) if old)
    doomed = [profile_id for profile_id, _ in candidates if str(profile_id) not in keep]
    if not doomed:
        return 0
    result = await session.execute(
        delete(StyleProfile).where(
            StyleProfile.id.in_(doomed), StyleProfile.state == ProfileState.previous
        )
    )
    return _rowcount(result)


def register_purge_job(
    scheduler: Scheduler,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: Clock = utcnow,
) -> None:
    async def job() -> None:
        await purge_messages(session_factory, settings, clock())

    scheduler.register_job(JOB_NAME, JOB_SCHEDULE, job)
