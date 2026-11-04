"""specs/014 ``db/repos/decisions.py``: one row per user decision, the rate the offer is based on,
the outcomes the warning is based on, and the 31-day purge. Synthetic data only."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scribe.db.models import Draft, DraftDecision, DraftDecisionKind, User, WorkChat
from scribe.db.repos import decisions, drafts, turns
from tests.conftest import FROZEN_NOW, MakeChat, MakeContact, MakeUser

NOW = FROZEN_NOW
SENT = DraftDecisionKind.sent_unchanged


async def _draft(
    db: AsyncSession, user: User, chat: WorkChat, contact_id: uuid.UUID, version: int = 1
) -> Draft:
    turn = await turns.create_turn(
        db,
        user.id,
        work_chat_id=chat.id,
        contact_id=contact_id,
        first_message_id=None,
        arrived_at=NOW,
        delay_s=0,
        due_at=NOW,
    )
    return await drafts.create_draft(
        db,
        user.id,
        conversation_turn_id=turn.id,
        text="synthetic draft",
        output_json={},
        prepared_at=NOW,
        version=version,
    )


async def _record(
    db: AsyncSession,
    user: User,
    chat: WorkChat,
    decision: DraftDecisionKind,
    *,
    draft: Draft | None = None,
    during: bool = False,
    at_offset: timedelta = timedelta(0),
) -> None:
    await decisions.record(
        db,
        user_id=user.id,
        work_chat_id=chat.id,
        draft_id=draft.id if draft is not None else None,
        decision=decision,
        during_auto_send=during,
        at=NOW + at_offset,
    )


async def test_record_inserts_one_row(
    db_session: AsyncSession, make_user: MakeUser, make_chat: MakeChat
) -> None:
    user = await make_user()
    chat = await make_chat(user)
    await _record(db_session, user, chat, DraftDecisionKind.rejected, during=True)
    rows = (await db_session.execute(select(DraftDecision))).scalars().all()
    assert [(r.user_id, r.work_chat_id, r.decision, r.during_auto_send, r.at) for r in rows] == [
        (user.id, chat.id, DraftDecisionKind.rejected, True, NOW)
    ]


async def test_chat_rate_counts_decisions_in_the_window_without_auto_rows(
    db_session: AsyncSession, make_user: MakeUser, make_chat: MakeChat
) -> None:
    user = await make_user()
    chat, other = await make_chat(user), await make_chat(user)
    since = NOW - timedelta(days=30)
    assert await decisions.chat_rate(
        db_session, user_id=user.id, work_chat_id=chat.id, since=since
    ) == (0, 0)

    for kind in (SENT, SENT, DraftDecisionKind.sent_edited, DraftDecisionKind.rejected):
        await _record(db_session, user, chat, kind)
    await _record(db_session, user, chat, DraftDecisionKind.regenerated)
    await _record(db_session, user, chat, DraftDecisionKind.auto_sent, during=True)
    await _record(db_session, user, chat, DraftDecisionKind.auto_cancelled, during=True)
    await _record(db_session, user, chat, SENT, at_offset=-timedelta(days=31))  # outside
    await _record(db_session, user, chat, SENT, at_offset=-timedelta(days=30))  # exactly since
    await _record(db_session, user, other, SENT)

    rate = await decisions.chat_rate(db_session, user_id=user.id, work_chat_id=chat.id, since=since)
    assert (rate.unchanged, rate.total) == (2, 5)
    assert rate == (2, 5)


async def test_rows_of_another_users_chat_are_never_counted(
    db_session: AsyncSession, make_user: MakeUser, make_chat: MakeChat
) -> None:
    mine, theirs = await make_user(), await make_user()
    chat, foreign = await make_chat(mine), await make_chat(theirs)
    await _record(db_session, theirs, foreign, SENT)
    since = NOW - timedelta(days=30)
    # The same chat id with another user's id never matches, and the other way round.
    args = {"since": since}
    assert await decisions.chat_rate(db_session, user_id=mine.id, work_chat_id=chat.id, **args) == (
        0,
        0,
    )
    assert await decisions.chat_rate(
        db_session, user_id=mine.id, work_chat_id=foreign.id, **args
    ) == (0, 0)
    assert await decisions.chat_rate(
        db_session, user_id=theirs.id, work_chat_id=foreign.id, **args
    ) == (1, 1)


async def test_recent_auto_outcomes_one_boolean_per_draft_newest_first(
    db_session: AsyncSession, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> None:
    user = await make_user()
    chat = await make_chat(user)
    contact = await make_contact(user)

    async def draft(minutes: int, *kinds: DraftDecisionKind, during: bool = True) -> None:
        stored = await _draft(db_session, user, chat, contact.id)
        for number, kind in enumerate(kinds):
            await _record(
                db_session,
                user,
                chat,
                kind,
                draft=stored,
                during=during,
                at_offset=timedelta(minutes=minutes, seconds=number),
            )

    await draft(1, DraftDecisionKind.auto_sent)  # not an intervention
    await draft(2, DraftDecisionKind.auto_cancelled, DraftDecisionKind.sent_edited)  # one draft
    await draft(3, SENT)  # Send now without changes
    await draft(4, DraftDecisionKind.auto_cancelled, DraftDecisionKind.rejected)
    await draft(5, DraftDecisionKind.regenerated)
    await draft(6, DraftDecisionKind.rejected, during=False)  # no countdown: not counted
    await draft(7, DraftDecisionKind.sent_edited)

    outcomes = await decisions.recent_auto_outcomes(
        db_session, user_id=user.id, work_chat_id=chat.id, limit=10
    )
    assert outcomes == [True, True, True, False, True, False]
    assert await decisions.recent_auto_outcomes(
        db_session, user_id=user.id, work_chat_id=chat.id, limit=2
    ) == [
        True,
        True,
    ]


async def test_purge_older_than_deletes_only_older_rows(
    db_session: AsyncSession, make_user: MakeUser, make_chat: MakeChat
) -> None:
    user = await make_user()
    chat = await make_chat(user)
    for days in (40, 32, 31, 29, 0):
        await _record(db_session, user, chat, SENT, at_offset=-timedelta(days=days))
    cutoff = NOW - timedelta(days=31)
    assert await decisions.purge_older_than(db_session, cutoff) == 2
    left = await db_session.scalar(select(func.count()).select_from(DraftDecision))
    assert left == 3
