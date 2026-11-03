"""Work account mode (specs/008, research R1-R8): every eligible private chat is a work chat,
minus the chats the user excludes.

- **Eligible** (R3): a one-to-one chat with a real person (``chat_kind == "private"`` and
  ``peer_kind == "user"``); never bots, Saved Messages, service or deleted accounts, never
  groups. :func:`is_eligible` is the only place that decides it.
- **Materialised flag** (R1): ``work_chats.enabled`` stays the single effective flag every
  reader uses (intake, outgoing handling, the composer's ``FOR SHARE`` check, earlier-conversation
  search, style import and refresh, ``/skipped``). ``work_chats.origin`` says why a chat is a work
  chat (``picked`` by the user, or ``auto``: picked up by the mode) and ``work_chats.excluded`` is
  the user's own "off" for an ``auto`` chat. This module keeps the invariant

      origin = 'auto'  ->  enabled = users.work_account AND NOT excluded

  and is the only writer of ``origin``, ``excluded``, ``users.work_account`` and of ``enabled``
  for ``auto`` chats. ``picked`` chats keep their meaning: ``enabled`` is the user's choice.
- **Lazy pick-up** (R2): :func:`pick_up` creates an ``auto`` row when a message (either
  direction) arrives in an eligible private chat without a row while the mode is on, the style
  import finds own messages in such a chat (:func:`materialise_imported`), or the user excludes
  a chat that never wrote (:func:`exclude_chat` with ``dialog``). Nothing else creates rows.
- **Lock order** (R4): the user row, then the chat row, then the chat's pending drafts, then
  their turns. Pick-up reads the user row ``FOR SHARE``; :func:`switch` updates it
  ``FOR UPDATE`` and commits before it sweeps the chats one short transaction each
  (:func:`deactivate_chat`, the former ``settings.disable_chat`` protocol, generalised).
- **Repair** (R8): :func:`reconcile_user` re-applies the invariant; it runs after every switch
  and at worker start for users who switched within the last day.

History: a picked-up chat is recorded with 007's ``chats_added`` kind (meta
``{"work_chat_ids": [id], "origin": "auto"}``); exclusions and mode switches have their own
kinds. Logs carry ids and counts only, never titles or message text.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.db.models import (
    ActivityKind,
    ChatKind,
    ConversationTurn,
    Draft,
    DraftState,
    DroppedReason,
    TurnState,
    User,
    WorkChat,
    WorkChatOrigin,
)
from scribe.db.repos import activity as activity_repo
from scribe.db.repos import chats as chats_repo
from scribe.logging import get_logger
from scribe.style.importer import ImportResult, ImportTarget
from scribe.telegram.gateway import Dialog, DialogKind, MessagingGateway, PeerKind

log = get_logger("scribe.chats")

_DEACTIVATE_ATTEMPTS = 5
_KINDS: dict[str, ChatKind] = {
    "private": ChatKind.private,
    "group": ChatKind.group,
    "channel_comments": ChatKind.channel_comments,
}


# --- eligibility ----------------------------------------------------------------------------------


def is_eligible(chat_kind: DialogKind | str | None, peer_kind: PeerKind | str | None) -> bool:
    """True only for a private chat with a real person (research R3). A missing ``peer_kind``
    is never eligible."""
    return chat_kind == "private" and peer_kind == "user"


async def _mode(
    session: AsyncSession, user_id: uuid.UUID, *, lock: Literal["share", "update"] | None
) -> bool | None:
    """``users.work_account`` (None: no such user), optionally locking the user row."""
    query = select(User.work_account).where(User.id == user_id)
    if lock == "share":
        query = query.with_for_update(read=True)
    elif lock == "update":
        query = query.with_for_update()
    found = (await session.execute(query)).first()
    return None if found is None else bool(found[0])


async def _record_added(
    session: AsyncSession, user_id: uuid.UUID, chat: WorkChat, now: datetime, source: str
) -> None:
    await activity_repo.record(
        session,
        user_id,
        ActivityKind.chats_added,
        at=now,
        meta={"work_chat_ids": [str(chat.id)], "origin": WorkChatOrigin.auto.value, "via": source},
    )


# --- pick-up --------------------------------------------------------------------------------------


async def pick_up(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    telegram_chat_id: int,
    title: str,
    chat_kind: DialogKind,
    peer_kind: PeerKind | None,
    direction: Literal["in", "out"],
    now: datetime,
) -> WorkChat | None:
    """Called by intake / outgoing handling when the chat has no ``work_chats`` row. Returns a
    new enabled ``auto`` row (and records ``chats_added``) iff the chat is eligible and the
    user's mode is on, read ``FOR SHARE`` in the caller's transaction; else None. Idempotent
    (insert-only on the unique ``(user_id, telegram_chat_id)``)."""
    if not is_eligible(chat_kind, peer_kind):
        if chat_kind == "private":
            log.debug("work_account_ineligible", user_id=str(user_id), peer_kind=peer_kind)
        return None
    if not await _mode(session, user_id, lock="share"):
        return None
    chat, created = await chats_repo.create_auto_chat(
        session,
        user_id,
        telegram_chat_id=telegram_chat_id,
        title=title,
        kind=ChatKind.private,
        enabled=True,
        excluded=False,
    )
    if created:
        await _record_added(session, user_id, chat, now, "message")
        log.info(
            "work_chat_picked_up",
            user_id=str(user_id),
            work_chat_id=str(chat.id),
            direction=direction,
        )
    return chat


# --- deactivate (the disable protocol) ------------------------------------------------------------


@dataclass(frozen=True)
class Deactivated:
    dropped_draft_ids: list[uuid.UUID] = field(default_factory=list)
    dropped_waiting: int = 0
    changed: bool = False  # the chat row was written (False: skipped, nothing to do)


class _DraftAppeared(Exception):
    """A draft was stored in the chat after its pending drafts were locked: start over."""


async def deactivate_chat(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    work_chat_id: uuid.UUID,
    now: datetime,
    *,
    reason: DroppedReason = DroppedReason.chat_disabled,
    set_excluded: bool | None = None,
    activity: tuple[ActivityKind, dict[str, Any]] | None = None,
    only_if_mode_off: bool = False,
) -> Deactivated | None:
    """Set the chat's ``enabled = false`` (and ``excluded`` when ``set_excluded`` is given) in
    one transaction and drop its waiting turns and pending drafts with ``reason``; None: no
    such chat.

    Lock order (data-model.md): the chat row (the composer reads it ``FOR SHARE`` before it
    stores a draft, so a draft is never stored in a chat being deactivated), then the chat's
    pending drafts, then their turns. Pending drafts are dropped with their turns; waiting turns
    are dropped by one conditional ``UPDATE … WHERE state = 'waiting'``, so a turn the scheduler
    claimed meanwhile (``reading``) is never written over: the composer ends it without a draft.

    ``only_if_mode_off`` (the turn-off sweep): the user row is read ``FOR SHARE`` first and the
    chat is left alone unless the mode is still off and the chat is an enabled ``auto`` chat.
    ``activity`` is recorded in the same transaction."""
    for _ in range(_DEACTIVATE_ATTEMPTS):
        try:
            return await _deactivate_once(
                session_factory,
                user_id,
                work_chat_id,
                now,
                reason=reason,
                set_excluded=set_excluded,
                activity=activity,
                only_if_mode_off=only_if_mode_off,
            )
        except _DraftAppeared:
            continue
    raise RuntimeError("chat deactivation kept racing with new drafts")


async def _deactivate_once(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    work_chat_id: uuid.UUID,
    now: datetime,
    *,
    reason: DroppedReason,
    set_excluded: bool | None,
    activity: tuple[ActivityKind, dict[str, Any]] | None,
    only_if_mode_off: bool,
) -> Deactivated | None:
    chat_turns = select(ConversationTurn.id).where(
        ConversationTurn.user_id == user_id, ConversationTurn.work_chat_id == work_chat_id
    )
    async with session_factory() as session:
        if only_if_mode_off and await _mode(session, user_id, lock="share") is not False:
            return Deactivated()
        chat = (
            await session.execute(
                select(WorkChat)
                .where(WorkChat.user_id == user_id, WorkChat.id == work_chat_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if chat is None:
            return None
        if only_if_mode_off and (
            chat.origin != WorkChatOrigin.auto or chat.excluded or not chat.enabled
        ):
            return Deactivated()
        chat.enabled = False
        if set_excluded is not None:
            chat.excluded = set_excluded
        drafts = list(
            (
                await session.execute(
                    select(Draft)
                    .where(
                        Draft.user_id == user_id,
                        Draft.state == DraftState.pending,
                        Draft.conversation_turn_id.in_(chat_turns),
                    )
                    .order_by(Draft.id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        turns = {
            turn.id: turn
            for turn in (
                await session.execute(
                    select(ConversationTurn)
                    .where(
                        ConversationTurn.user_id == user_id,
                        ConversationTurn.id.in_([d.conversation_turn_id for d in drafts]),
                    )
                    .order_by(ConversationTurn.id)
                    .with_for_update()
                )
            ).scalars()
        }
        for draft in drafts:
            draft.state = DraftState.dropped
            draft.finalized_at = now
            turn = turns.get(draft.conversation_turn_id)
            if turn is not None and turn.state == TurnState.pending_review:
                turn.state = TurnState.dropped
                turn.dropped_reason = reason
            await activity_repo.record(
                session,
                user_id,
                ActivityKind.dropped,
                at=now,
                draft_id=draft.id,
                conversation_turn_id=draft.conversation_turn_id,
                meta={"reason": reason.value},
            )
        waiting = await session.execute(
            update(ConversationTurn)
            .where(
                ConversationTurn.user_id == user_id,
                ConversationTurn.work_chat_id == work_chat_id,
                ConversationTurn.state == TurnState.waiting,
            )
            .values(state=TurnState.dropped, dropped_reason=reason)
            .returning(ConversationTurn.id)
        )
        waiting_ids = list(waiting.scalars().all())
        for turn_id in waiting_ids:
            await activity_repo.record(
                session,
                user_id,
                ActivityKind.dropped,
                at=now,
                conversation_turn_id=turn_id,
                meta={"reason": reason.value},
            )
        if activity is not None:
            await activity_repo.record(session, user_id, activity[0], at=now, meta=activity[1])
        appeared = await session.scalar(
            select(func.count())
            .select_from(Draft)
            .where(
                Draft.user_id == user_id,
                Draft.state == DraftState.pending,
                Draft.conversation_turn_id.in_(chat_turns),
            )
        )
        if appeared:
            await session.rollback()
            raise _DraftAppeared
        await session.commit()
    return Deactivated([draft.id for draft in drafts], len(waiting_ids), True)


# --- exclude / include ----------------------------------------------------------------------------


async def exclude_chat(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    *,
    work_chat_id: uuid.UUID | None = None,
    dialog: Dialog | None = None,
    now: datetime,
) -> list[uuid.UUID] | None:
    """Exclude a chat (research R5) and return the ids of the pending drafts it dropped (their
    cards get a status line); None: no such chat.

    - An ``auto`` chat: ``excluded = true``, ``enabled = false``.
    - A ``picked`` chat: the plain disable (``enabled = false``).
    - ``dialog`` without a row (a chat that never wrote): an ``auto`` row holding only the
      Telegram id, title and kind, ``excluded = true`` (no contact, no messages).

    Waiting turns and pending drafts are dropped with ``chat_disabled``; ``chat_excluded`` is
    recorded. Excluding an already excluded chat changes nothing."""
    if work_chat_id is None:
        if dialog is None:
            return None
        async with session_factory() as session:
            chat, created = await chats_repo.create_auto_chat(
                session,
                user_id,
                telegram_chat_id=dialog.chat_id,
                title=dialog.title,
                kind=_KINDS.get(dialog.kind, ChatKind.group),
                enabled=False,
                excluded=True,
            )
            if created:
                await activity_repo.record(
                    session,
                    user_id,
                    ActivityKind.chat_excluded,
                    at=now,
                    meta={"work_chat_id": str(chat.id)},
                )
            await session.commit()
        if created:
            _log_exclusion("work_chat_excluded", user_id, chat)
            return []
        work_chat_id = chat.id
    async with session_factory() as session:
        chat_or_none = await chats_repo.get_work_chat(session, user_id, work_chat_id)
    if chat_or_none is None:
        return None
    chat = chat_or_none
    if chats_repo.is_excluded(chat) or (chat.origin == WorkChatOrigin.picked and not chat.enabled):
        return []
    auto = chat.origin == WorkChatOrigin.auto
    result = await deactivate_chat(
        session_factory,
        user_id,
        chat.id,
        now,
        reason=DroppedReason.chat_disabled,
        set_excluded=True if auto else None,
        activity=(ActivityKind.chat_excluded, {"work_chat_id": str(chat.id)}),
    )
    if result is None:
        return None
    _log_exclusion("work_chat_excluded", user_id, chat)
    return result.dropped_draft_ids


async def include_chat(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    work_chat_id: uuid.UUID,
    *,
    now: datetime,
) -> bool:
    """Include an excluded chat again: an ``auto`` chat gets ``excluded = false`` and
    ``enabled = users.work_account``; a ``picked`` chat ``enabled = true``. Records
    ``chat_included`` when something changed. False: no such chat."""
    async with session_factory() as session:
        mode = await _mode(session, user_id, lock="share")
        chat = (
            await session.execute(
                select(WorkChat)
                .where(WorkChat.user_id == user_id, WorkChat.id == work_chat_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if chat is None or mode is None:
            return False
        if chat.origin == WorkChatOrigin.auto:
            changed = chat.excluded or chat.enabled != mode
            chat.excluded = False
            chat.enabled = mode
        else:
            changed = not chat.enabled
            chat.enabled = True
        if changed:
            await activity_repo.record(
                session,
                user_id,
                ActivityKind.chat_included,
                at=now,
                meta={"work_chat_id": str(chat.id)},
            )
        await session.commit()
    if changed:
        _log_exclusion("work_chat_included", user_id, chat)
    return True


def _log_exclusion(event: str, user_id: uuid.UUID, chat: WorkChat) -> None:
    log.info(event, user_id=str(user_id), work_chat_id=str(chat.id), origin=chat.origin.value)


# --- the switch -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class SwitchResult:
    changed: bool  # False: the mode already was in the requested state, nothing done
    paused_chats: int = 0
    dropped_draft_ids: list[uuid.UUID] = field(default_factory=list)
    dropped_waiting: int = 0
    enabled_chats: int = 0


async def switch(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    *,
    on: bool,
    now: datetime,
) -> SwitchResult:
    """Turn the mode on or off (research R4).

    The flag (``users.work_account``, ``work_account_changed_at``) is updated under
    ``SELECT … FOR UPDATE`` and committed first, so no chat is picked up after it. On: the
    ``auto`` chats that are not excluded are enabled in the same transaction. Off: each enabled
    ``auto`` chat is then deactivated in its own transaction (reason ``work_account_off``; their
    waiting turns and pending drafts are dropped); ``picked`` chats are never touched.
    ``work_account_on`` / ``work_account_off`` is recorded once."""
    async with session_factory() as session:
        user = (
            await session.execute(select(User).where(User.id == user_id).with_for_update())
        ).scalar_one_or_none()
        if user is None or user.work_account == on:
            return SwitchResult(changed=False)
        user.work_account = on
        user.work_account_changed_at = now
        enabled = 0
        if on:
            enabled = await _enable_auto_chats(session, user_id)
        kind = ActivityKind.work_account_on if on else ActivityKind.work_account_off
        await activity_repo.record(session, user_id, kind, at=now)
        await session.commit()
    if on:
        log.info(
            "work_account_switched",
            user_id=str(user_id),
            on=True,
            enabled_chats=enabled,
            paused_chats=0,
            dropped_drafts=0,
            dropped_waiting=0,
        )
        return SwitchResult(changed=True, enabled_chats=enabled)
    paused, drafts, waiting = await _sweep_off(session_factory, user_id, now)
    log.info(
        "work_account_switched",
        user_id=str(user_id),
        on=False,
        paused_chats=paused,
        dropped_drafts=len(drafts),
        dropped_waiting=waiting,
    )
    return SwitchResult(
        changed=True, paused_chats=paused, dropped_draft_ids=drafts, dropped_waiting=waiting
    )


async def _enable_auto_chats(session: AsyncSession, user_id: uuid.UUID) -> int:
    result = await session.execute(
        update(WorkChat)
        .where(
            WorkChat.user_id == user_id,
            WorkChat.origin == WorkChatOrigin.auto,
            WorkChat.excluded.is_(False),
            WorkChat.enabled.is_(False),
        )
        .values(enabled=True)
        .returning(WorkChat.id)
    )
    return len(result.scalars().all())


async def _sweep_off(
    session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID, now: datetime
) -> tuple[int, list[uuid.UUID], int]:
    """Deactivate every enabled ``auto`` chat, one transaction each, in id order."""
    async with session_factory() as session:
        ids = list(
            (
                await session.execute(
                    select(WorkChat.id)
                    .where(
                        WorkChat.user_id == user_id,
                        WorkChat.origin == WorkChatOrigin.auto,
                        WorkChat.enabled.is_(True),
                    )
                    .order_by(WorkChat.id)
                )
            )
            .scalars()
            .all()
        )
    paused, drafts, waiting = 0, [], 0
    for work_chat_id in ids:
        result = await deactivate_chat(
            session_factory,
            user_id,
            work_chat_id,
            now,
            reason=DroppedReason.work_account_off,
            only_if_mode_off=True,
        )
        if result is None or not result.changed:
            continue
        paused += 1
        drafts.extend(result.dropped_draft_ids)
        waiting += result.dropped_waiting
    return paused, drafts, waiting


@dataclass(frozen=True)
class OffPreview:
    chats: int  # enabled ``auto`` chats that would be paused
    pending_drafts: int
    waiting: int


async def preview_off(session: AsyncSession, user_id: uuid.UUID) -> OffPreview:
    """The counts the turn-off confirmation shows."""
    auto_chats = select(WorkChat.id).where(
        WorkChat.user_id == user_id,
        WorkChat.origin == WorkChatOrigin.auto,
        WorkChat.enabled.is_(True),
    )
    chats = await session.scalar(select(func.count()).select_from(auto_chats.subquery()))
    turns_of = select(ConversationTurn.id).where(
        ConversationTurn.user_id == user_id, ConversationTurn.work_chat_id.in_(auto_chats)
    )
    drafts = await session.scalar(
        select(func.count())
        .select_from(Draft)
        .where(
            Draft.user_id == user_id,
            Draft.state == DraftState.pending,
            Draft.conversation_turn_id.in_(turns_of),
        )
    )
    waiting = await session.scalar(
        select(func.count())
        .select_from(ConversationTurn)
        .where(
            ConversationTurn.user_id == user_id,
            ConversationTurn.state == TurnState.waiting,
            ConversationTurn.work_chat_id.in_(auto_chats),
        )
    )
    return OffPreview(int(chats or 0), int(drafts or 0), int(waiting or 0))


async def reconcile_user(
    session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID, *, now: datetime
) -> int:
    """Re-apply the ``auto`` invariant (research R8) and return the number of chats fixed:
    enable the ones that should be on, deactivate the ones that should be off (an interrupted
    turn-off sweep, or an excluded chat left enabled). Idempotent."""
    async with session_factory() as session:
        mode = await _mode(session, user_id, lock="share")
        if mode is None:
            return 0
        fixed = await _enable_auto_chats(session, user_id) if mode else 0
        await session.commit()
    async with session_factory() as session:
        wrong = list(
            (
                await session.execute(
                    select(WorkChat.id, WorkChat.excluded)
                    .where(
                        WorkChat.user_id == user_id,
                        WorkChat.origin == WorkChatOrigin.auto,
                        WorkChat.enabled.is_(True),
                    )
                    .order_by(WorkChat.id)
                )
            ).all()
        )
    for work_chat_id, excluded in wrong:
        if excluded:
            result = await deactivate_chat(
                session_factory, user_id, work_chat_id, now, reason=DroppedReason.chat_disabled
            )
        elif not mode:
            result = await deactivate_chat(
                session_factory,
                user_id,
                work_chat_id,
                now,
                reason=DroppedReason.work_account_off,
                only_if_mode_off=True,
            )
        else:
            continue
        if result is not None and result.changed:
            fixed += 1
    if fixed:
        log.info("work_account_reconciled", user_id=str(user_id), fixed=fixed)
    return fixed


async def users_to_reconcile(session: AsyncSession, since: datetime) -> list[uuid.UUID]:
    """Users whose mode changed at or after ``since`` (the startup reconcile, research R8)."""
    result = await session.execute(
        select(User.id).where(User.work_account_changed_at >= since).order_by(User.id)
    )
    return list(result.scalars().all())


RECONCILE_WINDOW = timedelta(days=1)


async def reconcile_recent(
    session_factory: async_sessionmaker[AsyncSession], *, now: datetime
) -> int:
    """Worker start (research R8): :func:`reconcile_user` for every user whose mode changed
    within the last day, so a turn-off sweep cut short by a restart is finished. Returns the
    number of chats fixed; a failing user is logged and skipped."""
    async with session_factory() as session:
        user_ids = await users_to_reconcile(session, now - RECONCILE_WINDOW)
    fixed = 0
    for user_id in user_ids:
        try:
            fixed += await reconcile_user(session_factory, user_id, now=now)
        except Exception as exc:
            log.warning(
                "work_account_reconcile_failed", user_id=str(user_id), error=type(exc).__name__
            )
    log.info("work_account_startup_reconcile", users=len(user_ids), fixed=fixed)
    return fixed


# --- style import ---------------------------------------------------------------------------------


async def import_targets(
    session: AsyncSession, gateway: MessagingGateway, user_id: uuid.UUID, *, max_auto: int
) -> list[ImportTarget]:
    """The chats the style import reads (research R6): every enabled work chat, plus — while the
    mode is on — up to ``max_auto`` of the most recent eligible private dialogs that have no
    row yet (Telegram lists dialogs newest first; ``list_dialogs`` already leaves out bots,
    Saved Messages, service and deleted accounts). Excluded and paused chats and unpicked groups
    are never read."""
    chats = await chats_repo.list_work_chats(session, user_id)
    targets = [
        ImportTarget(chat.telegram_chat_id, chat.title, chat.kind, chat.id)
        for chat in chats
        if chat.enabled
    ]
    if max_auto <= 0 or not await _mode(session, user_id, lock=None):
        return targets
    try:
        dialogs = await gateway.list_dialogs(user_id)
    except Exception as exc:
        log.warning(
            "work_account_import_dialogs_failed", user_id=str(user_id), error=type(exc).__name__
        )
        return targets
    known = {chat.telegram_chat_id for chat in chats}
    extra: list[ImportTarget] = []
    for dialog in dialogs:
        if len(extra) >= max_auto:
            break
        if dialog.kind != "private" or dialog.chat_id in known:
            continue
        extra.append(ImportTarget(dialog.chat_id, dialog.title, ChatKind.private, None))
    return targets + extra


async def materialise_imported(
    session: AsyncSession,
    user_id: uuid.UUID,
    imported: ImportResult,
    targets: Sequence[ImportTarget],
    *,
    now: datetime,
) -> int:
    """Create enabled ``auto`` rows (``chats_added``) for the imported dialogs without a row in
    which the user wrote at least one message, so their contacts rank for profiles. Nothing is
    created if the mode was turned off meanwhile. The caller commits; returns the rows created."""
    wanted = [
        target
        for target in targets
        if target.work_chat_id is None and imported.per_chat_counts.get(target.telegram_chat_id)
    ]
    if not wanted or not await _mode(session, user_id, lock="share"):
        return 0
    created_count = 0
    for target in wanted:
        chat, created = await chats_repo.create_auto_chat(
            session,
            user_id,
            telegram_chat_id=target.telegram_chat_id,
            title=target.title,
            kind=ChatKind.private,
            enabled=True,
            excluded=False,
        )
        if created:
            created_count += 1
            await _record_added(session, user_id, chat, now, "import")
    if created_count:
        log.info("work_chats_materialised", user_id=str(user_id), created=created_count)
    return created_count


__all__ = [
    "Deactivated",
    "OffPreview",
    "SwitchResult",
    "deactivate_chat",
    "exclude_chat",
    "import_targets",
    "include_chat",
    "is_eligible",
    "materialise_imported",
    "pick_up",
    "preview_off",
    "reconcile_recent",
    "reconcile_user",
    "switch",
    "users_to_reconcile",
]
