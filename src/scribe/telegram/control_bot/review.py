"""Draft review in the control chat (contracts/control-chat.md "Draft card"; FR-011 to FR-014).

Callbacks ``approve|edit|regen|reject|sources:<draft_id>[:confirm]``. ``approve`` and ``edit``
are the only paths that can lead to sending, and only through ``sending.send_approved_draft``
after setting ``approved_by_user_at`` (Principle I). Every callback first loads the draft scoped
to the calling user, so nobody can act on another user's draft. Expired, superseded or
otherwise finished drafts answer "This draft has expired" and do nothing.

US2: an ``outdated`` draft's card carries the "⚠️ New messages arrived" banner and the buttons
🔁 Regenerate / **Send anyway**. "Send anyway" is ``approve:<draft_id>:anyway``: the same
approval path as ✅ Send (including the sensitive / placeholder confirmation, then
``approve:<draft_id>:anyway_confirm``), and the only approve that may send an outdated draft.
A plain ``approve:<draft_id>[:confirm]`` of an outdated draft (an old card, a card whose
re-render failed) sends nothing and asks "Regenerate or Send anyway?"; ``send.py`` enforces
the same rule under the row lock. Editing is the user's own text, so it may be sent for an
outdated draft. Regenerating an outdated draft first marks the chat read up to the newest
message of the turn, but never past the first message of another turn that is still waiting
in that chat (specs/009: and marks it unread right after that read). ``readnow:<turn_id>``
makes a waiting turn due now; the scheduler reads and drafts it on its next tick. Reminders
and expiry (``scheduling/deadlines.py``) reply to and close cards through
:class:`DraftPresenter`. Reject finalizes only a still-pending draft, under its row lock;
anything else answers "This draft was already handled". The send callbacks pass the
``unread_marker`` (specs/009) to ``send_approved_draft``.

Issue #2: a needs-info card lists its placeholder questions numbered. A plain text reply to
that card, or a plain message while it is the newest pending card, is taken as the missing
values (``drafting/placeholders.py``: the whole message for one placeholder, one value per line
otherwise). The filled text becomes a new draft version with a new card for the usual review;
it is never sent by this. A wrong number of values is explained and changes nothing. Any FSM
state (setup, login, settings questions, a draft edit) takes precedence, and messages starting
with ``/`` are never values. A filled version carries the values as ``edited_text``, so ✅ Send
sends it as an edited reply.

US3: sources ``task:<id>`` expand to the task's title, due date and status (from the task
source's per-user cache, refreshed for 🧾 Sources when needed; nothing from the task source is
stored), ``commit:<uuid>`` to the user's promise with its due date; conflicts show the title of
the task or commitment they clash with. When a draft was prepared without task context and the
connection has turned ``error`` (repeated failures), the user gets one alert per failure streak
pointing to /tasks (the row is locked while the alert flag is checked and set). The stored model
output is read through ``drafting.stored_output.load_stored_output`` (content encrypted at rest).

US4: the card names the profile the draft used (the profile's own contact, or "global"). After an
edited send, ``style.proposals.propose_after_edit`` may post a 💡 rule proposal
(``rule_accept`` / ``rule_dismiss``, handled in ``profile_command.py``). ``OwnershipMiddleware``
refuses a callback naming another user's draft or turn before any handler runs.

specs/014 (auto-send): in a chat the user opted in to, the card of an eligible draft shows
"will be sent automatically at HH:MM" and the keyboard of :func:`render.armed_draft_keyboard`;
``autostop:<draft_id>`` (⏹) cancels the countdown and keeps the draft. The countdown is armed
only here, in :meth:`DraftPresenter.post_card`, after the card was posted, in the transaction
that stores ``control_message_id`` (``sending/auto_send.py``, research A4a). Every other card
action (approve, edit, reject, regenerate, fill) stops the countdown first, under the draft's
row lock; once the scheduler step has claimed the draft they answer "Already being sent
automatically" and do nothing, so exactly one of the user and the countdown wins.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.access.apply import no_access_reply
from scribe.clock import Clock, utcnow
from scribe.config import Settings
from scribe.context.commitments import parse_commitment_ref
from scribe.context.earlier import EARLIER_PREFIX, EarlierSearch
from scribe.context.history import parse_message_ref, turn_messages
from scribe.context.tasks.base import Deadline, Task
from scribe.context.tasks.service import ALERTED_KEY, default_task_sources, parse_task_ref
from scribe.db.models import (
    ActivityKind,
    AutoSendStop,
    ConnectionStatus,
    ConversationTurn,
    Draft,
    DraftDecisionKind,
    DraftState,
    ProfileScope,
    TurnState,
    User,
    UserState,
)
from scribe.db.repos import activity as activity_repo
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import commitments as commitments_repo
from scribe.db.repos import decisions as decisions_repo
from scribe.db.repos import drafts as drafts_repo
from scribe.db.repos import messages as messages_repo
from scribe.db.repos import profiles as profiles_repo
from scribe.db.repos import tasks_conn as tasks_conn_repo
from scribe.db.repos import turns as turns_repo
from scribe.db.repos import users as users_repo
from scribe.drafting.budget import DraftingPaused, paused_alert, paused_now
from scribe.drafting.composer import GATE_SYSTEM_WARNINGS, Composer, DraftNoLongerPending
from scribe.drafting.injection import hard_stops
from scribe.drafting.placeholders import (
    PLACEHOLDER,
    ValueCountMismatch,
    draft_slots,
    parse_values,
    store_filled_version,
)
from scribe.drafting.selection import regenerations
from scribe.drafting.stored_output import StoredEarlier, load_stored_output
from scribe.i18n import (
    Lang,
    Msg,
    format_day_month,
    format_day_month_year,
    format_weekday_date,
    format_when,
    render,
    t,
)
from scribe.intake.waiting import first_waiting_message_ids, readable_up_to
from scribe.llm.provider import DraftOutput
from scribe.llm.registry import LLMRouter
from scribe.logging import get_logger
from scribe.scheduling.deadlines import expire_draft
from scribe.sending import auto_send
from scribe.sending.auto_send import DecisionAdvisor
from scribe.sending.send import (
    AccessSuspended,
    DraftOutdated,
    SendAttemptUnresolved,
    SendRefused,
    send_approved_draft,
    send_attempt_unresolved,
)
from scribe.style import proposals
from scribe.telegram.control_bot.edit_prefill import edited_text_of, send_edit_prompt
from scribe.telegram.control_bot.ownership import OwnershipMiddleware
from scribe.telegram.control_bot.render import (
    CallbackData,
    DraftCardView,
    InvalidCallbackData,
    armed_draft_keyboard,
    button,
    confirm_send_keyboard,
    draft_keyboard,
    format_callback,
    keyboard,
    numbered,
    parse_callback,
    render_draft_card,
)
from scribe.telegram.gateway import AlreadyDelivered, MessagingGateway
from scribe.telegram.unread_mark import UnreadMarker

log = get_logger("scribe.control_bot")

_PLACEHOLDER = PLACEHOLDER
FILLED_STATUS = Msg("status.filled")
QUOTE_CHARS = 40
ANYWAY = "anyway"  # approve:<draft_id>:anyway — the outdated card's "Send anyway"
ANYWAY_CONFIRM = "anyway_confirm"  # ... and its sensitive / placeholder confirmation
TASKS_ERROR_ALERT = Msg("review.tasks_error_alert")
PROVIDER_LABELS = {"trello": "Trello", "jira": "Jira"}  # product names, the same in every language
SUPERGROUP_ID_BASE = 10**12  # Bot API supergroup ids are -100<internal id>


class ReviewStates(StatesGroup):
    editing = State()


# --- card view ----------------------------------------------------------------------------------


def _local(moment: datetime, tz: str) -> datetime:
    return moment.astimezone(ZoneInfo(tz))


def _hhmm(moment: datetime, tz: str) -> str:
    return _local(moment, tz).strftime("%H:%M")


def draft_output_of(draft: Draft) -> tuple[DraftOutput | None, list[str]]:
    """The stored model output (decrypted) and the system warnings (``drafts.output_json``)."""
    stored = load_stored_output(draft.output_json, draft.text)
    return stored.output, stored.system_warnings


def _quote(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= QUOTE_CHARS else flat[: QUOTE_CHARS - 1].rstrip() + "…"


def _due_suffix(due: Any, lang: str) -> str:
    if due is None:
        return ""
    return t("review.due_suffix", lang, day=format_weekday_date(due, lang))


def _deadline_note(deadline: Deadline, timezone: str, lang: str) -> str:
    """Where a comment / description deadline comes from, with the quote (🧾 Sources): 'from a
    comment by Ivan Petrov, Sun 18 Oct: “moving to Nov 5”'. The model gets the English
    ``deadlines.deadline_note``; this is the user's text, in their language."""
    if deadline.source == "description":
        return t("review.deadline_description_quote", lang, quote=deadline.quote)
    by = t("review.deadline_by", lang, author=deadline.author) if deadline.author else ""
    when = (
        t(
            "review.deadline_when",
            lang,
            day=format_weekday_date(deadline.noted_at.astimezone(ZoneInfo(timezone)).date(), lang),
        )
        if deadline.noted_at is not None
        else ""
    )
    return t("review.deadline_comment_quote", lang, by=by, when=when, quote=deadline.quote)


def _task_due_suffix(task: Task, timezone: str, lang: str, *, detailed: bool = False) -> str:
    """`` (due Thu 5 Nov)``; a deadline from a comment or the description says so (with the
    quote when ``detailed``), issue #6."""
    due = task.due_on(timezone)
    deadline = task.deadline
    if due is None or deadline is None or deadline.source == "due":
        return _due_suffix(due, lang)
    day = format_weekday_date(due, lang)
    if detailed:
        note = _deadline_note(deadline, timezone, lang)
        return t("review.due_suffix_noted", lang, day=day, note=note)
    if deadline.source == "description":
        note = t("review.deadline_description", lang)
    else:
        by = t("review.deadline_by", lang, author=deadline.author) if deadline.author else ""
        note = t("review.deadline_comment", lang, by=by)
    return t("review.due_suffix_noted", lang, day=day, note=note)


def _task_label(task: Task, timezone: str, lang: str = "en", *, detailed: bool) -> str:
    provider = PROVIDER_LABELS.get(task.source, task.source)
    # A Jira issue is named by its key too: `Jira PAY-142 “Fix refund rounding”` (specs/003).
    if task.source == "jira":
        provider = f"{provider} {task.id}"
    label = t(
        "review.source_task",
        lang,
        provider=provider,
        title=task.title,
        due=_task_due_suffix(task, timezone, lang, detailed=detailed),
    )
    return f"{label} · {task.status}" if detailed and task.status else label


def _tasks_by_ref(user: User, tasks: list[Task] | None) -> dict[str, Task]:
    found = (
        tasks if tasks is not None else default_task_sources().cached_tasks(user.id, user.timezone)
    )
    return {task.id: task for task in found}


async def _ref_label(
    session: AsyncSession,
    user: User,
    kind: str,
    ref: str,
    tasks: dict[str, Task],
    *,
    detailed: bool,
    short: bool = False,
) -> str | None:
    """Readable label of a cited message, task or commitment (None: not found)."""
    lang = user.language
    if kind == "message":
        message_id = parse_message_ref(ref)
        message = (
            await messages_repo.get_message(session, user.id, message_id) if message_id else None
        )
        if message is None:
            return None
        when = format_day_month(_local(message.sent_at, user.timezone), lang)
        who = t("review.source_own", lang)
        if not message.is_outgoing and message.sender_contact_id is not None:
            contact = await chats_repo.get_contact(session, user.id, message.sender_contact_id)
            who = contact.display_name if contact is not None else t("review.source_message", lang)
        if message.is_forwarded:  # #47: someone else's words, not the forwarder's
            who = (
                t("review.source_forwarded", lang, who=who, origin=message.forwarded_from)
                if message.forwarded_from
                else t("review.source_forwarded_unknown", lang, who=who)
            )
        return f"{who} {when} “{_quote(message.text)}”"
    if kind == "task":
        task_id = parse_task_ref(ref)
        task = tasks.get(task_id) if task_id is not None else None
        if task is None:
            return None
        if short:
            key = f"{task.id} " if task.source == "jira" else ""  # FR-J18: name the issue
            return f"{key}{task.title}{_task_due_suffix(task, user.timezone, lang)}"
        return _task_label(task, user.timezone, lang, detailed=detailed)
    if kind == "commitment":
        commitment_id = parse_commitment_ref(ref)
        commitment = (
            await commitments_repo.get_commitment(session, user.id, commitment_id)
            if commitment_id is not None
            else None
        )
        if commitment is None:
            return None
        if short:
            return f"{commitment.what}{_due_suffix(commitment.due, lang)}"
        label = t(
            "review.source_promise",
            lang,
            what=commitment.what,
            due=_due_suffix(commitment.due, lang),
        )
        if detailed and commitment.source_message_text:
            said = format_day_month(_local(commitment.source_sent_at, user.timezone), lang)
            label += t(
                "review.source_said",
                lang,
                day=said,
                quote=_quote(commitment.source_message_text),
            )
        return label
    return None


def earlier_link(chat_id: int, message_id: int) -> str | None:
    """``https://t.me/c/<internal id>/<message id>`` for a supergroup message, else None."""
    if chat_id > -SUPERGROUP_ID_BASE:
        return None
    return f"https://t.me/c/{-chat_id - SUPERGROUP_ID_BASE}/{message_id}"


def _earlier_label(item: StoredEarlier | None, user: User, used_for: str, *, detailed: bool) -> str:
    """A cited earlier message (specs/004 contracts/control-chat.md): ``💬 <chat> · <date>`` on
    the card; with sender, time, the note, the excerpt and a supergroup link in Sources."""
    lang = user.language
    if item is None:  # drafted before this feature, or the excerpt was purged
        label = t("review.source_earlier_missing", lang)
        return f"{label} — {used_for}" if detailed else label
    local = _local(item.sent_at, user.timezone)
    chat = item.chat_title or t("common.chat", lang)
    day = format_day_month_year(local, lang)
    if not detailed:
        return t("card.source_earlier", lang, chat=chat, day=day)
    sender = item.sender or t("common.someone", lang)
    head = t(
        "review.source_earlier", lang, chat=chat, sender=sender, day=day, time=f"{local:%H:%M}"
    )
    lines = [f"{head} — {used_for}"]
    excerpt = " ".join(item.excerpt.split())
    if excerpt:
        lines.append(f"  «{excerpt}»")
    link = earlier_link(item.chat_id, item.message_id)
    if link is not None:
        lines.append(f"  {link}")
    return "\n".join(lines)


async def source_labels(
    session: AsyncSession,
    user: User,
    draft: Draft,
    *,
    detailed: bool = False,
    tasks: list[Task] | None = None,
) -> list[str]:
    stored = load_stored_output(draft.output_json, draft.text)
    output = stored.output
    if output is None:
        return []
    earlier = {item.ref: item for item in stored.earlier_messages}
    by_ref = _tasks_by_ref(user, tasks)
    labels: list[str] = []
    for source in output.sources:
        if source.id.startswith(EARLIER_PREFIX):
            item = earlier.get(source.id)
            labels.append(_earlier_label(item, user, source.used_for, detailed=detailed))
            continue
        label = await _ref_label(session, user, source.kind, source.id, by_ref, detailed=detailed)
        if label is None:
            label = (
                t("review.source_task_unavailable", user.language)
                if source.kind == "task"
                else f"{source.kind} {source.id}"
            )
        labels.append(f"{label} — {source.used_for}" if detailed else label)
    return labels


async def conflict_labels(
    session: AsyncSession, user: User, draft: Draft, *, tasks: list[Task] | None = None
) -> list[tuple[str, str]]:
    """(what it conflicts with, the model's description) per conflict."""
    output, _ = draft_output_of(draft)
    if output is None:
        return []
    by_ref = _tasks_by_ref(user, tasks)
    labels = []
    for conflict in output.conflicts:
        label = await _ref_label(
            session, user, conflict.with_kind, conflict.with_id, by_ref, detailed=False, short=True
        )
        labels.append((label or conflict.description, conflict.description))
    return labels


async def load_card_view(
    session: AsyncSession,
    user: User,
    draft: Draft,
    *,
    now: datetime,
    status: Msg | None = None,
    auto_send_reasons: Sequence[str] = (),
    auto_send_stopped: bool = False,
) -> DraftCardView:
    """The card of ``draft``. A running countdown shows its end time; ``auto_send_reasons`` /
    ``auto_send_stopped`` (specs/014) say why a card in an auto-send chat waits for the user."""
    lang = user.language
    turn = await turns_repo.get_turn(session, user.id, draft.conversation_turn_id)
    if turn is None:
        raise LookupError("turn not found")
    chat = await chats_repo.get_work_chat(session, user.id, turn.work_chat_id)
    contact = await chats_repo.get_contact(session, user.id, turn.contact_id)
    incoming = [m.text for m in await turn_messages(session, turn)]
    profile_label = t("card.profile.default", lang)
    if draft.style_profile_id is not None:
        profile = await profiles_repo.get_profile(session, user.id, draft.style_profile_id)
        if profile is not None:
            scope = "global" if profile.scope == ProfileScope.global_ else "contact"
            profile_label = t(f"card.profile.{scope}", lang)
            if profile.scope == ProfileScope.contact and profile.contact_id is not None:
                # The profile's own contact (the person, or the group as a whole).
                owner = await chats_repo.get_contact(session, user.id, profile.contact_id)
                if owner is not None and owner.display_name:
                    profile_label = owner.display_name
    output, system_warnings = draft_output_of(draft)
    warnings: list[str] = list(output.context_warnings) if output is not None else []
    warnings += [w for w in system_warnings if w not in warnings]
    # Codes without a card text of their own are shown as "⚠️ <code>" (render.py).
    commitment = None
    if output is not None and output.flags.creates_commitment.value:
        commitment = output.flags.creates_commitment.what or t("card.commitment_something", lang)
        if output.flags.creates_commitment.due:
            due = output.flags.creates_commitment.due
            commitment = t("card.commitment_due", lang, what=commitment, due=due)
    return DraftCardView(
        contact_title=contact.display_name if contact is not None else "",
        chat_kind=chat.kind.value if chat is not None else "private",
        read_at=_hhmm(turn.read_at, user.timezone) if turn.read_at is not None else None,
        incoming=incoming,
        profile_label=profile_label,
        tone_shift=output.tone_shift_applied if output is not None else "none",
        draft_text=draft.text or "",
        expires=format_when(draft.expires_at, now, user.timezone, lang),
        commitment=commitment,
        conflicts=[label for label, _ in await conflict_labels(session, user, draft)],
        sensitive=bool(output and output.flags.sensitive.value),
        sensitive_categories=list(output.flags.sensitive.categories) if output else [],
        needs_info=[slot.question for slot in draft_slots(draft)],
        needs_info_flag=bool(output and output.needs_info),
        sources=await source_labels(session, user, draft),
        warnings=warnings,
        status=render(status, lang) if status is not None else None,
        lang=lang,
        possibly_unnecessary=draft.possibly_unnecessary,
        auto_send_at=(
            _local(draft.auto_send_due_at, user.timezone)
            if draft.auto_send_due_at is not None and auto_send.is_armed(draft)
            else None
        ),
        auto_send_reasons=tuple(auto_send_reasons),
        auto_send_stopped=auto_send_stopped,
    )


class DraftPresenter:
    """Posts and updates draft cards in the user's control chat."""

    def __init__(
        self,
        bot: Bot,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Clock = utcnow,
    ) -> None:
        self._bot = bot
        self._session_factory = session_factory
        self._clock = clock

    async def post_card(self, user_id: uuid.UUID, draft_id: uuid.UUID) -> None:
        async with self._session_factory() as session:
            user = await users_repo.get_user(session, user_id)
            draft = await drafts_repo.get_draft(session, user_id, draft_id)
            if user is None or draft is None:
                return
            if user.access_suspended_at is not None:
                # specs/005: access ended while this draft was being prepared: never shown.
                if draft.state == DraftState.pending:
                    await expire_draft(session, draft, now=self._clock())
                    await session.commit()
                log.info(
                    "draft_card_skipped_suspended", user_id=str(user_id), draft_id=str(draft_id)
                )
                return
            now = self._clock()
            # specs/014 research A4a: the card is posted with the countdown the draft would get
            # (or the reasons it waits); nothing is armed until the post succeeded.
            plan = await auto_send.plan_arm(session, draft_id=draft_id, now=now)
            view = await load_card_view(session, user, draft, now=now)
            if plan is not None:
                view = _with_plan(view, plan, user.timezone)
            chat_id, outdated = user.telegram_control_chat_id, draft.outdated
            without_tasks = "tasks_unavailable" in draft_output_of(draft)[1]
        planned = plan is not None and plan.planned_due_at is not None
        sent = await self._bot.send_message(
            chat_id,
            card_text(view, outdated=outdated),
            reply_markup=card_keyboard(draft_id, outdated, view.lang, armed=planned),
        )
        stored_at = self._clock()
        armed = True
        async with self._session_factory() as session:
            await drafts_repo.update_draft(
                session, user_id, draft_id, control_message_id=sent.message_id
            )
            if plan is not None and planned:
                armed = await self._commit_arm(
                    session, user_id, draft_id, plan, sent.message_id, stored_at
                )
            await session.commit()
        log.info("draft_card_posted", user_id=str(user_id), draft_id=str(draft_id))
        if not armed:  # the card promised a countdown that was not started: say so (safe side)
            await self._without_countdown(user_id, draft_id)
        if without_tasks:
            await self._alert_task_source_error(user_id, chat_id, view.lang)

    @staticmethod
    async def _commit_arm(
        session: AsyncSession,
        user_id: uuid.UUID,
        draft_id: uuid.UUID,
        plan: auto_send.ArmPlan,
        control_message_id: int,
        stored_at: datetime,
    ) -> bool:
        """Start the countdown in the transaction that stores the card; a failure only costs the
        countdown (a savepoint keeps the card's own update)."""
        try:
            async with session.begin_nested():
                return await auto_send.commit_arm(
                    session,
                    draft_id=draft_id,
                    plan=plan,
                    control_message_id=control_message_id,
                    stored_at=stored_at,
                )
        except Exception as exc:
            log.error(
                "auto_send_arm_failed",
                user_id=str(user_id),
                draft_id=str(draft_id),
                error=type(exc).__name__,
            )
            return False

    async def _without_countdown(self, user_id: uuid.UUID, draft_id: uuid.UUID) -> None:
        try:
            await self.show_manual(user_id, draft_id)
        except Exception as exc:
            log.warning(
                "auto_send_card_update_failed", draft_id=str(draft_id), error=type(exc).__name__
            )

    async def _alert_task_source_error(self, user_id: uuid.UUID, chat_id: int, lang: str) -> None:
        """One alert when the task source turned ``error`` (cleared by a success or /tasks)."""
        try:
            async with self._session_factory() as session:
                conn = await tasks_conn_repo.get_connection_for_update(session, user_id)
                if conn is None or conn.status != ConnectionStatus.error:
                    return
                if (conn.config_json or {}).get(ALERTED_KEY):
                    return
                conn.config_json = {**(conn.config_json or {}), ALERTED_KEY: True}
                await session.commit()
            await self._bot.send_message(chat_id, TASKS_ERROR_ALERT.render(lang))
        except Exception as exc:
            log.warning("task_source_alert_failed", user_id=str(user_id), error=type(exc).__name__)

    async def show_status(self, user_id: uuid.UUID, draft_id: uuid.UUID, status: Msg) -> None:
        """Re-render the card with a final status line (in the user's language), no buttons."""
        async with self._session_factory() as session:
            user = await users_repo.get_user(session, user_id)
            draft = await drafts_repo.get_draft(session, user_id, draft_id)
            if user is None or draft is None or draft.control_message_id is None:
                return
            view = await load_card_view(session, user, draft, now=self._clock(), status=status)
            chat_id, message_id = user.telegram_control_chat_id, draft.control_message_id
        try:
            await self._bot.edit_message_text(
                text=render_draft_card(view), chat_id=chat_id, message_id=message_id
            )
        except Exception as exc:  # card too old to edit, deleted by the user, ...
            log.warning("draft_card_edit_failed", draft_id=str(draft_id), error=type(exc).__name__)
            await self._bot.send_message(chat_id, view.status or "")

    async def show_outdated(self, user_id: uuid.UUID, draft_id: uuid.UUID) -> None:
        """Re-render a pending card after a new message: with the "New messages arrived" banner
        (FR-015) while the draft is outdated, and the 💤 banner when a closing message says it
        may no longer be needed (004 FR-011)."""
        async with self._session_factory() as session:
            user = await users_repo.get_user(session, user_id)
            draft = await drafts_repo.get_draft(session, user_id, draft_id)
            if user is None or draft is None or draft.state != DraftState.pending:
                return
            view = await load_card_view(session, user, draft, now=self._clock())
            chat_id, message_id = user.telegram_control_chat_id, draft.control_message_id
            outdated, armed = draft.outdated, auto_send.is_armed(draft)
        await self._rerender(
            user_id,
            draft_id,
            chat_id,
            message_id,
            card_text(view, outdated=outdated),
            card_keyboard(draft_id, outdated=outdated, lang=view.lang, armed=armed),
        )

    async def show_manual(
        self,
        user_id: uuid.UUID,
        draft_id: uuid.UUID,
        reasons: Sequence[str] | None = None,
        *,
        stopped: bool = False,
    ) -> None:
        """specs/014: re-render a pending card as an ordinary one that waits for the user, with
        the line saying why (``reasons``: ``autosend.reason`` codes; ``None``: work them out
        now) or, with ``stopped``, that the user cancelled the countdown."""
        async with self._session_factory() as session:
            user = await users_repo.get_user(session, user_id)
            draft = await drafts_repo.get_draft(session, user_id, draft_id)
            if user is None or draft is None or draft.state != DraftState.pending:
                return
            if reasons is None and not stopped:
                found = await auto_send.current_reasons(session, draft_id)
                reasons = [r.value for r in found] or [AutoSendStop.ineligible.value]
            view = await load_card_view(
                session,
                user,
                draft,
                now=self._clock(),
                auto_send_reasons=tuple(reasons or ()),
                auto_send_stopped=stopped,
            )
            chat_id, message_id = user.telegram_control_chat_id, draft.control_message_id
            outdated = draft.outdated
        await self._rerender(
            user_id,
            draft_id,
            chat_id,
            message_id,
            card_text(view, outdated=outdated),
            card_keyboard(draft_id, outdated=outdated, lang=view.lang),
        )

    async def _rerender(
        self,
        user_id: uuid.UUID,
        draft_id: uuid.UUID,
        chat_id: int,
        message_id: int | None,
        text: str,
        markup: InlineKeyboardMarkup,
    ) -> None:
        """Edit the draft's card; when it cannot be edited (too old, deleted) post a new one."""
        if message_id is not None:
            try:
                await self._bot.edit_message_text(
                    text=text, chat_id=chat_id, message_id=message_id, reply_markup=markup
                )
                return
            except Exception as exc:  # card too old to edit, deleted by the user, ...
                log.warning(
                    "draft_card_edit_failed", draft_id=str(draft_id), error=type(exc).__name__
                )
        sent = await self._bot.send_message(chat_id, text, reply_markup=markup)
        async with self._session_factory() as session:
            await drafts_repo.update_draft(
                session, user_id, draft_id, control_message_id=sent.message_id
            )
            await session.commit()

    async def remind(self, user_id: uuid.UUID, draft_id: uuid.UUID, text: Msg) -> None:
        """Reply to the card (FR-032 reminder); never touches the contact's chat."""
        async with self._session_factory() as session:
            user = await users_repo.get_user(session, user_id)
            draft = await drafts_repo.get_draft(session, user_id, draft_id)
            if user is None or draft is None:
                return
            chat_id, message_id = user.telegram_control_chat_id, draft.control_message_id
            lang = user.language
        await self._bot.send_message(chat_id, text.render(lang), reply_to_message_id=message_id)

    async def sources_text(self, user_id: uuid.UUID, draft_id: uuid.UUID, lang: str) -> str:
        async with self._session_factory() as session:
            user = await users_repo.get_user(session, user_id)
            draft = await drafts_repo.get_draft(session, user_id, draft_id)
            if user is None or draft is None:
                return t("review.not_found", lang)
        tasks = await self._tasks_for(user, draft)
        async with self._session_factory() as session:
            labels = await source_labels(session, user, draft, detailed=True, tasks=tasks)
            conflicts = await conflict_labels(session, user, draft, tasks=tasks)
        lines = [f"• {label}" for label in labels]
        lines += [
            t("review.source_conflict", lang, what=label, why=why) for label, why in conflicts
        ]
        if not lines:
            return t("review.no_sources", lang)
        return t("review.sources", lang, sources="\n".join(lines))

    async def _tasks_for(self, user: User, draft: Draft) -> list[Task] | None:
        """Cached tasks, refreshed from the task source when a cited task is not cached."""
        output, _ = draft_output_of(draft)
        cited = {
            parse_task_ref(ref)
            for ref in [
                *(s.id for s in (output.sources if output else []) if s.kind == "task"),
                *(c.with_id for c in (output.conflicts if output else []) if c.with_kind == "task"),
            ]
        } - {None}
        sources = default_task_sources()
        cached = sources.cached_tasks(user.id, user.timezone)
        if not cited or cited <= {task.id for task in cached}:
            return cached
        loaded = await sources.load_open_tasks(self._session_factory, user.id)
        return loaded.tasks or cached


def card_text(view: DraftCardView, *, outdated: bool) -> str:
    card = render_draft_card(view)
    return f"{t('review.outdated_banner', view.lang)}\n\n{card}" if outdated else card


def _regen_and_send_anyway(draft_id: uuid.UUID, lang: str) -> list[InlineKeyboardButton]:
    return [
        button(t("btn.regen", lang), format_callback("regen", draft_id)),
        button(t("btn.send_anyway", lang), format_callback("approve", draft_id, ANYWAY)),
    ]


def outdated_keyboard(draft_id: uuid.UUID, lang: str) -> InlineKeyboardMarkup:
    """contracts/control-chat.md: Send is replaced by 🔁 Regenerate and **Send anyway**."""
    return keyboard(
        [
            _regen_and_send_anyway(draft_id, lang),
            [
                button(t("btn.edit", lang), format_callback("edit", draft_id)),
                button(t("btn.reject", lang), format_callback("reject", draft_id)),
                button(t("btn.sources", lang), format_callback("sources", draft_id)),
            ],
        ]
    )


def card_keyboard(
    draft_id: uuid.UUID, outdated: bool, lang: str, *, armed: bool = False
) -> InlineKeyboardMarkup:
    """``armed``: a countdown is running (specs/014); an outdated draft is never armed."""
    if outdated:
        return outdated_keyboard(draft_id, lang)
    return armed_draft_keyboard(draft_id, lang) if armed else draft_keyboard(draft_id, lang)


def _with_plan(view: DraftCardView, plan: auto_send.ArmPlan, timezone: str) -> DraftCardView:
    """The card as it is posted in an auto-send chat: the planned end of the window or the
    reasons the draft waits for the user."""
    if plan.planned_due_at is not None:
        return dataclasses.replace(view, auto_send_at=_local(plan.planned_due_at, timezone))
    return dataclasses.replace(view, auto_send_reasons=tuple(r.value for r in plan.reasons))


def outdated_prompt_keyboard(draft_id: uuid.UUID, lang: str) -> InlineKeyboardMarkup:
    return keyboard([_regen_and_send_anyway(draft_id, lang)])


# --- helpers ------------------------------------------------------------------------------------


def _is_live(draft: Draft, now: datetime) -> bool:
    return draft.state == DraftState.pending and draft.expires_at > now


async def _claim_card_action(
    session_factory: async_sessionmaker[AsyncSession], user: User, draft: Draft, now: datetime
) -> bool:
    """specs/014 research A3: the user acts on a card, so its countdown stops, under the draft's
    row lock. False: the scheduler step already claimed the draft and it is being sent
    automatically; the caller answers "too late" and does nothing."""
    outcome = await auto_send.stop_for_user(
        session_factory, user_id=user.id, draft_id=draft.id, now=now
    )
    return outcome is not None


def _needs_confirmation(draft: Draft) -> list[str]:
    """Catalog keys of the reasons to confirm before sending."""
    output, system_warnings = draft_output_of(draft)
    reasons = []
    if output is None or output.needs_info or _PLACEHOLDER.search(draft.text or ""):
        reasons.append("review.reason.placeholders")
    if output is None or output.flags.sensitive.value:
        reasons.append("review.reason.sensitive")
    # Issue #23: the injection hard stops (system-side, the model cannot clear them). An
    # automatic send must refuse these drafts outright via ``injection.hard_stops``.
    reasons += [f"review.reason.{code}" for code in hard_stops(system_warnings)]
    return reasons


def _confirm_text(reasons: list[str], lang: str) -> str:
    joined = t(reasons[0], lang)
    for reason in reasons[1:]:
        joined = t("review.reason_and", lang, first=joined, second=t(reason, lang))
    return t("review.confirm_send", lang, reasons=joined)


async def _owned_draft(
    callback: CallbackQuery,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
) -> tuple[CallbackData, Draft] | None:
    if user is None:
        await callback.answer(t("common.not_set_up", lang))
        return None
    try:
        data = parse_callback(callback.data)
        draft_id = uuid.UUID(data.entity_id)
    except (InvalidCallbackData, ValueError):
        await callback.answer(t("review.not_found", lang))
        return None
    async with session_factory() as session:
        draft = await drafts_repo.get_draft(session, user.id, draft_id)  # ownership check
    if draft is None:
        log.warning("review_callback_foreign_or_unknown_draft", user_id=str(user.id))
        await callback.answer(t("review.not_found", lang))
        return None
    return data, draft


async def _advise(
    advisor: DecisionAdvisor | None,
    session_factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    draft: Draft,
) -> None:
    """specs/014 research A7: after a decision committed, let the advisor weigh the offer and the
    suggestion to turn auto-send off. Nothing here can fail the action that triggered it."""
    if advisor is None:
        return
    try:
        async with session_factory() as session:
            work_chat_id = await auto_send.work_chat_id_of(session, draft)
        await advisor.after_decision(user_id, work_chat_id)
    except Exception as exc:
        log.warning("autosend_advisor_call_failed", error=type(exc).__name__)


async def _approve_and_send(
    *,
    bot: Bot,
    user: User,
    draft: Draft,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: MessagingGateway,
    clock: Clock,
    edited_text: str | None = None,
    allow_outdated: bool = False,
    unread_marker: UnreadMarker | None = None,
    advisor: DecisionAdvisor | None = None,
) -> bool:
    """Record the explicit approval of this draft, then send through ``send.py``."""
    lang = user.language
    now = clock()
    async with session_factory() as session:
        stored = await drafts_repo.get_draft(session, user.id, draft.id)
        if stored is None or not _is_live(stored, now):
            await bot.send_message(user.telegram_control_chat_id, t("review.expired", lang))
            return False
        if edited_text is None and stored.edited_text is not None:
            edited_text = stored.edited_text  # a version with placeholders filled by the user
        stored.approved_by_user_at = now
        kind = ActivityKind.approved if edited_text is None else ActivityKind.edited_and_approved
        await activity_repo.record(
            session, user.id, kind, at=now, draft_id=draft.id,
            conversation_turn_id=draft.conversation_turn_id,
        )  # fmt: skip
        await session.commit()
    try:
        result = await send_approved_draft(
            session_factory,
            gateway,
            user_id=user.id,
            draft_id=draft.id,
            edited_text=edited_text,
            allow_outdated=allow_outdated,
            clock=clock,
            marker=unread_marker,
        )
    except DraftOutdated:  # flagged after the button was pressed: ask again, send nothing
        log.info("send_refused_outdated", draft_id=str(draft.id))
        await bot.send_message(
            user.telegram_control_chat_id,
            t("review.outdated_prompt", lang),
            reply_markup=outdated_prompt_keyboard(draft.id, lang),
        )
        return False
    except AccessSuspended:  # access ended after the card was shown (specs/005)
        log.info("send_refused_access_suspended", draft_id=str(draft.id))
        async with session_factory() as session:
            text, markup = await no_access_reply(
                session, user.telegram_control_chat_id, has_account=True, lang=lang, now=clock()
            )
        await bot.send_message(user.telegram_control_chat_id, text, reply_markup=markup)
        return False
    except AlreadyDelivered:  # Telegram: an earlier attempt was delivered (#54)
        log.info("send_already_delivered", draft_id=str(draft.id))
        await bot.send_message(user.telegram_control_chat_id, t("review.already_delivered", lang))
        return False
    except SendAttemptUnresolved:  # a changed text after an attempt that may be delivered (#54)
        log.info("send_refused_attempt_unresolved", draft_id=str(draft.id))
        await bot.send_message(user.telegram_control_chat_id, t("review.send_unresolved", lang))
        return False
    except SendRefused as exc:
        log.warning("send_refused", draft_id=str(draft.id), reason=str(exc))
        await bot.send_message(user.telegram_control_chat_id, t("review.expired", lang))
        return False
    except Exception as exc:
        log.error("send_failed", draft_id=str(draft.id), error=type(exc).__name__)
        await bot.send_message(user.telegram_control_chat_id, t("review.send_failed", lang))
        return False
    status = "status.sent_edited" if result.edited else "status.sent"
    presenter = DraftPresenter(bot, session_factory, clock=clock)
    await presenter.show_status(
        user.id, draft.id, Msg(status, time=_hhmm(result.sent_at, user.timezone))
    )
    if result.edited:
        await _propose_rules(bot, user, draft.id, session_factory)
    await _advise(advisor, session_factory, user.id, draft)
    return True


async def _propose_rules(
    bot: Bot,
    user: User,
    draft_id: uuid.UUID,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """FR-033: after an edited send, offer a style rule when the edits repeat a pattern. The
    reply is already sent; nothing here can fail or undo it."""
    try:
        notices = await proposals.propose_after_edit(
            session_factory, user.id, draft_id, lang=user.language
        )
        for notice in notices:
            await bot.send_message(
                user.telegram_control_chat_id,
                notice.text.render(user.language),
                reply_markup=proposal_keyboard(notice.proposal_id, user.language),
            )
    except Exception as exc:
        log.error("rule_proposal_failed", draft_id=str(draft_id), error=type(exc).__name__)


def proposal_keyboard(proposal_id: uuid.UUID, lang: str) -> InlineKeyboardMarkup:
    return keyboard(
        [
            [
                button(t("btn.add_rule", lang), format_callback("rule_accept", proposal_id)),
                button(t("btn.dismiss", lang), format_callback("rule_dismiss", proposal_id)),
            ]
        ]
    )


# --- handlers -----------------------------------------------------------------------------------


async def on_approve(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: MessagingGateway,
    lang: Lang,
    clock: Clock = utcnow,
    unread_marker: UnreadMarker | None = None,
    auto_send_advisor: DecisionAdvisor | None = None,
) -> None:
    owned = await _owned_draft(callback, user, session_factory, lang)
    if owned is None or user is None:
        return
    data, draft = owned
    if not _is_live(draft, clock()):
        await callback.answer(t("review.expired", lang))
        return
    if not await _claim_card_action(session_factory, user, draft, clock()):
        await callback.answer(t("autosend.too_late", lang))
        return
    anyway = data.extra in (ANYWAY, ANYWAY_CONFIRM)
    if draft.outdated and not anyway:  # only "Send anyway" may send an outdated draft
        await callback.answer()
        await bot.send_message(
            user.telegram_control_chat_id,
            t("review.outdated_prompt", lang),
            reply_markup=outdated_prompt_keyboard(draft.id, lang),
        )
        return
    reasons = _needs_confirmation(draft)
    if reasons and data.extra not in ("confirm", ANYWAY_CONFIRM):
        await callback.answer()
        await bot.send_message(
            user.telegram_control_chat_id,
            _confirm_text(reasons, lang),
            reply_markup=(
                keyboard(
                    [
                        [
                            button(
                                t("btn.send_anyway", lang),
                                format_callback("approve", draft.id, ANYWAY_CONFIRM),
                            )
                        ]
                    ]
                )
                if anyway
                else confirm_send_keyboard("approve", draft.id, lang)
            ),
        )
        return
    await callback.answer(t("review.sending", lang))
    await _approve_and_send(
        bot=bot, user=user, draft=draft, session_factory=session_factory, gateway=gateway,
        clock=clock, allow_outdated=anyway, unread_marker=unread_marker,
        advisor=auto_send_advisor,
    )  # fmt: skip


async def on_edit(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    gateway: MessagingGateway,
    lang: Lang,
    clock: Clock = utcnow,
    unread_marker: UnreadMarker | None = None,
    auto_send_advisor: DecisionAdvisor | None = None,
) -> None:
    owned = await _owned_draft(callback, user, session_factory, lang)
    if owned is None or user is None:
        return
    data, draft = owned
    if not _is_live(draft, clock()):
        await callback.answer(t("review.expired", lang))
        await state.clear()
        return
    if send_attempt_unresolved(draft):  # #54: the earlier text may already be delivered
        await state.clear()
        await callback.answer()
        await bot.send_message(user.telegram_control_chat_id, t("review.send_unresolved", lang))
        return
    if data.extra == "confirm":  # "Send anyway" for an edited text that kept placeholders
        fsm = await state.get_data()
        edited = fsm.get("edited_text")
        await state.clear()
        if fsm.get("draft_id") != str(draft.id) or not isinstance(edited, str):
            await callback.answer(t("review.expired", lang))
            return
        await callback.answer(t("review.sending", lang))
        await _approve_and_send(
            bot=bot, user=user, draft=draft, session_factory=session_factory, gateway=gateway,
            clock=clock, edited_text=edited, allow_outdated=True, unread_marker=unread_marker,
            advisor=auto_send_advisor,
        )  # fmt: skip
        return
    if not await _claim_card_action(session_factory, user, draft, clock()):
        await state.clear()
        await callback.answer(t("autosend.too_late", lang))
        return
    await state.set_state(ReviewStates.editing)
    await state.set_data({"draft_id": str(draft.id)})
    await callback.answer()
    await send_edit_prompt(  # the draft comes along: an input-field, copy or code-block prompt
        bot,
        settings,
        user.telegram_control_chat_id,
        draft.text or "",
        reply_to_message_id=draft.control_message_id,
        lang=lang,
    )


async def on_edited_text(
    message: Message,
    bot: Bot,
    state: FSMContext,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: MessagingGateway,
    lang: Lang,
    clock: Clock = utcnow,
    unread_marker: UnreadMarker | None = None,
    auto_send_advisor: DecisionAdvisor | None = None,
) -> None:
    fsm = await state.get_data()
    text = await edited_text_of(bot, message.text or "")  # never saves the inline "@bot "
    if user is None or not text.strip():
        return
    try:
        draft_id = uuid.UUID(str(fsm.get("draft_id")))
    except ValueError:
        await state.clear()
        return
    async with session_factory() as session:
        draft = await drafts_repo.get_draft(session, user.id, draft_id)
    if draft is None or not _is_live(draft, clock()):
        await state.clear()
        await message.answer(t("review.expired", lang))
        return
    if send_attempt_unresolved(draft):  # #54: the earlier text may already be delivered
        await state.clear()
        await message.answer(t("review.send_unresolved", lang))
        return
    if _PLACEHOLDER.search(text):
        await state.update_data(edited_text=text)
        await message.answer(
            t("review.placeholders_left", lang),
            reply_markup=confirm_send_keyboard("edit", draft.id, lang),
        )
        return
    await state.clear()
    await _approve_and_send(
        bot=bot, user=user, draft=draft, session_factory=session_factory, gateway=gateway,
        clock=clock, edited_text=text, allow_outdated=True, unread_marker=unread_marker,
        advisor=auto_send_advisor,
    )  # fmt: skip


def _fill_mismatch_text(questions: list[str], got: int, lang: str) -> str:
    expected = len(questions)
    need = (
        t("review.fill_need_one", lang)
        if expected == 1
        else t("review.fill_need_many", lang, n=expected)
    )
    return t("review.fill_mismatch", lang, need=need, questions=numbered(questions), got=got)


async def fill_target(
    message: Message,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock = utcnow,
) -> bool | dict[str, Any]:
    """Filter: the draft a plain message fills, as ``fill_draft``.

    A reply to one of the user's draft cards targets that card (the handler explains when it
    cannot be filled). Any other message targets the newest pending card, and only when it is
    live and has placeholders; otherwise the message is not for review and passes on.
    """
    if user is None:
        return False
    replied = message.reply_to_message
    async with session_factory() as session:
        draft = (
            await drafts_repo.get_by_control_message(session, user.id, replied.message_id)
            if replied is not None
            else None
        )
        if draft is not None:
            return {"fill_draft": draft}
        draft = await drafts_repo.get_latest_pending_card(session, user.id)
    if draft is None or not _is_live(draft, clock()) or not draft_slots(draft):
        return False
    return {"fill_draft": draft}


async def on_fill_values(
    message: Message,
    bot: Bot,
    user: User | None,
    fill_draft: Draft,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    """Issue #2: the missing values for a needs-info draft -> a new version and card."""
    draft = fill_draft
    if user is None:
        return
    if not _is_live(draft, clock()):
        await message.answer(t("review.expired", lang))
        return
    if send_attempt_unresolved(draft):  # #54: the earlier text may already be delivered
        await message.answer(t("review.send_unresolved", lang))
        return
    slots = draft_slots(draft)
    if not slots:
        await message.answer(t("review.nothing_to_fill", lang))
        return
    try:
        values = parse_values(message.text or "", len(slots))
    except ValueCountMismatch as exc:
        log.info("fill_values_mismatch", draft_id=str(draft.id), expected=exc.expected, got=exc.got)
        await message.answer(
            _fill_mismatch_text([slot.question for slot in slots], exc.got, lang),
            reply_to_message_id=draft.control_message_id,
        )
        return
    if not await _claim_card_action(session_factory, user, draft, clock()):
        await message.answer(t("autosend.too_late", lang))
        return
    try:
        new = await store_filled_version(
            session_factory,
            user_id=user.id,
            draft_id=draft.id,
            values=values,
            deadlines_s=settings.review_deadlines_s,
            clock=clock,
        )
    except DraftNoLongerPending:
        await message.answer(t("review.expired", lang))
        return
    except ValueCountMismatch:  # the draft changed between the check and the row lock
        await message.answer(t("review.expired", lang))
        return
    presenter = DraftPresenter(bot, session_factory, clock=clock)
    await presenter.show_status(user.id, draft.id, FILLED_STATUS)
    await presenter.post_card(user.id, new.id)


async def on_cancel_edit(message: Message, state: FSMContext, lang: Lang) -> None:
    await state.clear()
    await message.answer(t("review.edit_cancelled", lang))


async def on_reject(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
    clock: Clock = utcnow,
    auto_send_advisor: DecisionAdvisor | None = None,
) -> None:
    owned = await _owned_draft(callback, user, session_factory, lang)
    if owned is None or user is None:
        return
    _, draft = owned
    now = clock()
    async with session_factory() as session:
        # Row lock first (draft -> turn order): a concurrent send, drop or expiry is waited
        # for, and a draft it already finished is never overwritten with "rejected".
        stored = (
            await session.execute(
                select(Draft)
                .where(Draft.user_id == user.id, Draft.id == draft.id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if stored is None or stored.state in (DraftState.expired, DraftState.superseded):
            await callback.answer(t("review.expired", lang))
            return
        if stored.state != DraftState.pending:  # sent, dropped or rejected meanwhile
            await callback.answer(t("review.already_handled", lang))
            return
        if not _is_live(stored, now):
            await callback.answer(t("review.expired", lang))
            return
        if auto_send.is_firing(stored):  # the countdown got there first (specs/014 research A3)
            await callback.answer(t("autosend.too_late", lang))
            return
        if auto_send.stop_countdown(stored, AutoSendStop.user_action):
            await auto_send.note_stopped(session, stored, AutoSendStop.user_action, now)
        await decisions_repo.record(
            session,
            user_id=user.id,
            work_chat_id=await auto_send.work_chat_id_of(session, stored),
            draft_id=draft.id,
            decision=DraftDecisionKind.rejected,
            during_auto_send=stored.auto_send_due_at is not None,
            at=now,
        )
        await drafts_repo.finalize(session, user.id, draft.id, DraftState.rejected, at=now)
        await session.execute(
            update(ConversationTurn)
            .where(
                ConversationTurn.user_id == user.id,
                ConversationTurn.id == draft.conversation_turn_id,
                ConversationTurn.state == TurnState.pending_review,
            )
            .values(state=TurnState.rejected)
        )
        await activity_repo.record(
            session,
            user.id,
            ActivityKind.rejected,
            at=now,
            draft_id=draft.id,
            conversation_turn_id=draft.conversation_turn_id,
        )
        await session.commit()
    await callback.answer(t("review.rejected", lang))
    presenter = DraftPresenter(bot, session_factory, clock=clock)
    await presenter.show_status(user.id, draft.id, Msg("status.rejected"))
    await _advise(auto_send_advisor, session_factory, user.id, draft)


async def on_regen(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    gateway: MessagingGateway,
    lang: Lang,
    clock: Clock = utcnow,
    unread_marker: UnreadMarker | None = None,
    auto_send_advisor: DecisionAdvisor | None = None,
) -> None:
    owned = await _owned_draft(callback, user, session_factory, lang)
    if owned is None or user is None:
        return
    _, draft = owned
    if not _is_live(draft, clock()):
        await callback.answer(t("review.expired", lang))
        return
    if send_attempt_unresolved(draft):  # #54: a new version would be sent as a new message
        await callback.answer()
        await bot.send_message(user.telegram_control_chat_id, t("review.send_unresolved", lang))
        return
    # Issue #73: a cap on regenerates of the same messages (an outdated draft is never refused:
    # only a regenerate covers the messages that arrived after it).
    limit = settings.regenerate_max_per_draft
    if not draft.outdated and regenerations(draft.llm_usage_json) >= limit:
        log.info("regenerate_capped", draft_id=str(draft.id), limit=limit)
        await callback.answer(t("review.regen_limit", lang, n=limit), show_alert=True)
        return
    if await paused_now(session_factory, settings, user.id, clock()):  # issue #74
        alert = paused_alert(user.timezone, clock()).render(lang)
        await callback.answer(alert, show_alert=True)
        return
    if not await _claim_card_action(session_factory, user, draft, clock()):
        await callback.answer(t("autosend.too_late", lang), show_alert=True)
        return
    await callback.answer(t("review.regenerating", lang))
    if draft.outdated:
        await _mark_turn_read(session_factory, gateway, user, draft, unread_marker)
    composer = Composer(
        session_factory,
        llm_router,
        settings,
        clock=clock,
        earlier_search=EarlierSearch(gateway, settings, clock=clock),
    )
    _, old_warnings = draft_output_of(draft)
    try:
        new = await composer.compose(
            user.id,
            draft.conversation_turn_id,
            supersede_draft_id=draft.id,
            # Why the draft exists at all (004 reply gate) still holds for the new version.
            extra_system_warnings=[w for w in old_warnings if w in GATE_SYSTEM_WARNINGS],
        )
    except DraftNoLongerPending:
        await bot.send_message(user.telegram_control_chat_id, t("review.expired", lang))
        return
    except DraftingPaused:  # the hard cap was reached meanwhile (issue #74)
        alert = paused_alert(user.timezone, clock()).render(lang)
        await bot.send_message(user.telegram_control_chat_id, alert)
        return
    except Exception as exc:
        log.error("regenerate_failed", draft_id=str(draft.id), error=type(exc).__name__)
        await bot.send_message(user.telegram_control_chat_id, t("review.regen_failed", lang))
        return
    async with session_factory() as session:
        await activity_repo.record(
            session,
            user.id,
            ActivityKind.regenerated,
            at=clock(),
            draft_id=new.id,
            conversation_turn_id=new.conversation_turn_id,
            meta={"previous_draft_id": str(draft.id), "version": new.version},
        )
        await decisions_repo.record(  # specs/014: the superseded version was not good enough
            session,
            user_id=user.id,
            work_chat_id=await auto_send.work_chat_id_of(session, draft),
            draft_id=draft.id,
            decision=DraftDecisionKind.regenerated,
            during_auto_send=draft.auto_send_due_at is not None,
            at=clock(),
        )
        await session.commit()
    presenter = DraftPresenter(bot, session_factory, clock=clock)
    await presenter.show_status(user.id, draft.id, Msg("status.superseded"))
    await presenter.post_card(user.id, new.id)
    await _advise(auto_send_advisor, session_factory, user.id, draft)


async def on_autostop(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
    clock: Clock = utcnow,
    auto_send_advisor: DecisionAdvisor | None = None,
) -> None:
    """``autostop:<draft_id>`` (⏹ Don't send automatically, specs/014 FR-008): cancel the
    countdown, keep the draft pending and approvable. Writes an ``auto_cancelled`` decision."""
    owned = await _owned_draft(callback, user, session_factory, lang)
    if owned is None or user is None:
        return
    _, draft = owned
    if draft.state != DraftState.pending:
        await callback.answer(t("review.already_handled", lang))
        return
    if not _is_live(draft, clock()):
        await callback.answer(t("review.expired", lang))
        return
    stopped = await auto_send.stop_for_user(
        session_factory, user_id=user.id, draft_id=draft.id, now=clock(), cancelled=True
    )
    if stopped is None:
        await callback.answer(t("autosend.too_late", lang))
        return
    await callback.answer()
    if stopped:
        presenter = DraftPresenter(bot, session_factory, clock=clock)
        await presenter.show_manual(user.id, draft.id, stopped=True)
        await _advise(auto_send_advisor, session_factory, user.id, draft)


async def on_sources(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    owned = await _owned_draft(callback, user, session_factory, lang)
    if owned is None or user is None:
        return
    _, draft = owned
    await callback.answer()
    presenter = DraftPresenter(bot, session_factory, clock=clock)
    await bot.send_message(
        user.telegram_control_chat_id,
        await presenter.sources_text(user.id, draft.id, lang),
        reply_to_message_id=draft.control_message_id,
    )


async def _mark_turn_read(
    session_factory: async_sessionmaker[AsyncSession],
    gateway: MessagingGateway,
    user: User,
    draft: Draft,
    marker: UnreadMarker | None = None,
) -> None:
    """Read the chat up to the turn's newest message before drafting it again (best effort),
    but never past the first message of another turn still waiting in that chat. With a
    marker, the chat is marked unread right after the read (specs/009), decided before it."""
    async with session_factory() as session:
        turn = await turns_repo.get_turn(session, user.id, draft.conversation_turn_id)
        chat = await chats_repo.get_work_chat(session, user.id, turn.work_chat_id) if turn else None
        last = (
            await messages_repo.get_message(session, user.id, turn.last_message_id)
            if turn is not None and turn.last_message_id is not None
            else None
        )
        floors = (
            await first_waiting_message_ids(session, user.id, [chat.id]) if chat is not None else {}
        )
    if chat is None or last is None:
        return
    up_to = readable_up_to(last.telegram_message_id, floors.get(chat.id))
    if up_to < 1:
        return
    ticket = await marker.prepare(user.id, chat.telegram_chat_id, up_to) if marker else None
    try:
        await gateway.mark_read(user.id, chat.telegram_chat_id, up_to)
    except Exception as exc:
        log.warning("mark_read_failed", draft_id=str(draft.id), error=type(exc).__name__)
        return
    if marker is not None:
        await marker.commit(ticket)


async def on_read_now(
    callback: CallbackQuery,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    """``readnow:<turn_id>`` (FR-018): skip the remaining delay of one of the caller's turns."""
    if user is None:
        await callback.answer(t("common.not_set_up", lang))
        return
    try:
        turn_id = uuid.UUID(parse_callback(callback.data).entity_id)
    except (InvalidCallbackData, ValueError):
        await callback.answer(t("review.turn_not_waiting", lang))
        return
    now = clock()
    async with session_factory() as session:
        turn = await turns_repo.get_turn(session, user.id, turn_id)  # ownership check
        if turn is None or turn.state != TurnState.waiting:
            await callback.answer(t("review.turn_not_waiting", lang))
            return
        turn.due_at = now
        await activity_repo.record(
            session, user.id, ActivityKind.read_now, at=now, conversation_turn_id=turn.id
        )
        state = (await users_repo.get_user(session, user.id) or user).state
        await session.commit()
    log.info("read_now", user_id=str(user.id), turn_id=str(turn_id))
    await callback.answer(
        t("review.read_now_paused", lang)
        if state == UserState.paused
        else t("review.read_now", lang)
    )


def _action(name: str) -> Any:
    return F.data.regexp(rf"^{name}:")


def create_router() -> Router:
    router = Router(name="review")
    # T072: a callback naming another user's draft or turn stops here (handlers check too).
    router.callback_query.middleware(OwnershipMiddleware())
    router.callback_query.register(on_approve, _action("approve"))
    router.callback_query.register(on_edit, _action("edit"))
    router.callback_query.register(on_autostop, _action("autostop"))
    router.callback_query.register(on_reject, _action("reject"))
    router.callback_query.register(on_regen, _action("regen"))
    router.callback_query.register(on_sources, _action("sources"))
    router.callback_query.register(on_read_now, _action("readnow"))
    router.message.register(on_cancel_edit, Command("cancel"), StateFilter(ReviewStates.editing))
    # A command typed while editing is never taken as the reply text.
    router.message.register(
        on_edited_text, StateFilter(ReviewStates.editing), F.text & ~F.text.startswith("/")
    )
    # Issue #2: outside any flow, plain text for a needs-info card fills its placeholders.
    router.message.register(
        on_fill_values, StateFilter(None), F.text & ~F.text.startswith("/"), fill_target
    )
    return router
