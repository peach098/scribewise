"""30-day retention purge (T022). Synthetic data."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.access import applications
from scribe.access import repo as access_repo
from scribe.config import Settings
from scribe.db.models import (
    AccessEvent,
    AccessEventKind,
    BetaApplication,
    BotFsmState,
    Commitment,
    CommitmentState,
    DraftState,
    Message,
    ProfileScope,
    ProfileState,
    RefusedContact,
    ScheduledJob,
    StyleProfile,
)
from scribe.db.repos import commitments, drafts, messages, profiles, turns
from scribe.retention.purge import JOB_NAME, purge_messages, register_purge_job
from scribe.scheduling.scheduler import Scheduler
from tests.conftest import FrozenClock, MakeChat, MakeContact, MakeUser


async def test_purge_removes_only_expired_content(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    now = clock()
    old, recent = now - timedelta(days=31), now - timedelta(days=29)
    user = await make_user()
    chat, contact = await make_chat(user), await make_contact(user)

    old_message = await messages.add_message(
        db_session, user.id, work_chat_id=chat.id, telegram_message_id=1, text="old", sent_at=old
    )
    recent_message = await messages.add_message(
        db_session,
        user.id,
        work_chat_id=chat.id,
        telegram_message_id=2,
        text="recent",
        sent_at=recent,
    )
    turn = await turns.create_turn(
        db_session,
        user.id,
        work_chat_id=chat.id,
        contact_id=contact.id,
        first_message_id=old_message.id,
        arrived_at=old,
        delay_s=0,
        due_at=old,
    )

    async def draft(finalized_at: object, state: DraftState) -> object:
        row = await drafts.create_draft(
            db_session,
            user.id,
            conversation_turn_id=turn.id,
            text="synthetic draft",
            output_json={"kept": True},
            prepared_at=old,
        )
        row.edited_text = "synthetic edit"
        if finalized_at is not None:
            row.state, row.finalized_at = state, finalized_at  # type: ignore[assignment]
            if state == DraftState.sent:
                row.approved_by_user_at = old
        await db_session.flush()
        return row

    old_final = await draft(old, DraftState.sent)
    recent_final = await draft(recent, DraftState.rejected)
    pending = await draft(None, DraftState.pending)

    async def commitment(closed_at: object, state: CommitmentState) -> Commitment:
        row = await commitments.add_commitment(
            db_session,
            user.id,
            contact_id=contact.id,
            what="synthetic",
            due=None,
            source_sent_at=old,
            confidence=0.9,
            source_message_text="synthetic source",
        )
        row.state, row.closed_at = state, closed_at  # type: ignore[assignment]
        await db_session.flush()
        return row

    old_closed = await commitment(old, CommitmentState.resolved)
    recent_closed = await commitment(recent, CommitmentState.deleted)
    still_open = await commitment(None, CommitmentState.open)

    async def profile(state: ProfileState) -> StyleProfile:
        return await profiles.create_profile(
            db_session,
            user.id,
            scope=ProfileScope.global_,
            rules_json={},
            summary_text="",
            examples=[],
            sample_size=0,
            state=state,
        )

    old_retired, recent_retired, current = (
        await profile(ProfileState.retired),
        await profile(ProfileState.retired),
        await profile(ProfileState.current),
    )
    await db_session.flush()
    # updated_at is set by the database; age the "old" retired profile explicitly.
    old_retired.updated_at = old
    recent_retired.updated_at = recent
    current.updated_at = old
    await db_session.commit()
    recent_message_id = recent_message.id
    commitment_ids = (old_closed.id, recent_closed.id, still_open.id)
    profile_ids = (recent_retired.id, current.id)

    stats = await purge_messages(session_factory, settings, now)
    db_session.expire_all()

    remaining_messages = (await db_session.execute(select(Message.id))).scalars().all()
    assert remaining_messages == [recent_message_id]
    await db_session.refresh(turn)
    assert turn.first_message_id is None  # purged message reference is cleared, turn kept

    for row, redacted in [(old_final, True), (recent_final, False), (pending, False)]:
        await db_session.refresh(row)
        assert (row.text is None) is redacted  # type: ignore[attr-defined]
        assert (row.edited_text is None) is redacted  # type: ignore[attr-defined]
        assert row.output_json == {"kept": True}  # type: ignore[attr-defined]

    remaining_commitments = set((await db_session.execute(select(Commitment.id))).scalars().all())
    assert remaining_commitments == {commitment_ids[1], commitment_ids[2]}
    remaining_profiles = set((await db_session.execute(select(StyleProfile.id))).scalars().all())
    assert remaining_profiles == set(profile_ids)

    assert stats.messages_deleted == 1
    assert stats.drafts_redacted == 1
    assert stats.commitments_deleted == 1
    assert stats.profiles_deleted == 1

    again = await purge_messages(session_factory, settings, now)
    assert again.messages_deleted == again.drafts_redacted == 0


async def test_purge_is_registered_as_daily_job(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
) -> None:
    scheduler = Scheduler(session_factory, tick_s=5, clock=clock)
    register_purge_job(scheduler, session_factory, settings, clock)
    await scheduler.ensure_jobs()
    row = (
        await db_session.execute(select(ScheduledJob).where(ScheduledJob.name == JOB_NAME))
    ).scalar_one()
    assert row.next_run_at.hour == 3
    assert timedelta(0) < row.next_run_at - clock() <= timedelta(days=1)

    clock.set(row.next_run_at)
    await scheduler.tick()
    await scheduler.wait_idle()
    db_session.expire_all()
    await db_session.refresh(row)
    assert row.last_status == "ok"
    assert row.next_run_at - clock() == timedelta(days=1)


async def test_purge_removes_old_refused_contacts_and_access_events(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
) -> None:
    """specs/005 FR-007, research Q5/Q10: refused contacts go 30 days after they were last
    seen (not first seen), access events 365 days after they were written."""
    now = clock()
    for telegram_id, first_seen, last_seen in [
        (900000001, now - timedelta(days=60), now - timedelta(days=31)),  # purged
        (900000002, now - timedelta(days=60), now - timedelta(days=29)),  # seen again: kept
        (900000003, now - timedelta(days=1), now - timedelta(days=1)),  # kept
    ]:
        db_session.add(
            RefusedContact(
                telegram_id=telegram_id,
                first_seen_at=first_seen,
                last_seen_at=last_seen,
                attempts=2,
            )
        )
    for telegram_id, age in [(900000011, 366), (900000012, 364), (900000013, 0)]:
        await access_repo.record_event(
            db_session,
            telegram_id,
            AccessEventKind.beta_added,
            actor="operator",
            now=now - timedelta(days=age),
        )
    await db_session.commit()

    stats = await purge_messages(session_factory, settings, now)
    db_session.expire_all()

    refused = set((await db_session.execute(select(RefusedContact.telegram_id))).scalars().all())
    assert refused == {900000002, 900000003}
    events = set((await db_session.execute(select(AccessEvent.telegram_id))).scalars().all())
    assert events == {900000012, 900000013}
    assert stats.refused_contacts_deleted == 1
    assert stats.access_events_deleted == 1

    again = await purge_messages(session_factory, settings, now)
    assert again.refused_contacts_deleted == again.access_events_deleted == 0


async def test_purge_removes_decided_applications_and_abandoned_apply_flows(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: FrozenClock,
) -> None:
    """specs/005 FR-029, research Q12: decided applications go 30 days after the decision,
    pending ones stay; an unsent use case in ``ApplyStates`` goes after 24 h untouched."""
    now = clock()
    ids = {}
    for telegram_id, decided_days_ago in [
        (900000021, 31),  # purged
        (900000022, 29),  # kept
        (900000023, None),  # pending, submitted long ago: kept
    ]:
        application = await applications.submit(
            db_session,
            telegram_id=telegram_id,
            username=None,
            use_case="Synthetic use case text",
            language="en",
            now=now - timedelta(days=60),
        )
        if decided_days_ago is not None:
            await applications.decide(
                db_session,
                application.id,
                approve=False,
                operator_id=None,
                now=now - timedelta(days=decided_days_ago),
            )
        ids[telegram_id] = application.id
    for chat_id, state, age in [
        (900000031, "ApplyStates:use_case", timedelta(hours=25)),  # purged
        (900000032, "ApplyStates:review", timedelta(hours=23)),  # kept
        (900000033, "SettingsStates:delay", timedelta(days=3)),  # another flow: kept
    ]:
        db_session.add(BotFsmState(chat_id=chat_id, state=state, data={"use_case": "synthetic"}))
        await db_session.flush()
        await db_session.execute(
            update(BotFsmState).where(BotFsmState.chat_id == chat_id).values(updated_at=now - age)
        )
    await db_session.commit()

    stats = await purge_messages(session_factory, settings, now)
    db_session.expire_all()

    left = set((await db_session.execute(select(BetaApplication.telegram_id))).scalars().all())
    assert left == {900000022, 900000023}
    fsm = set((await db_session.execute(select(BotFsmState.chat_id))).scalars().all())
    assert {900000032, 900000033} <= fsm and 900000031 not in fsm
    assert stats.applications_deleted == 1
    assert stats.apply_flows_deleted == 1
