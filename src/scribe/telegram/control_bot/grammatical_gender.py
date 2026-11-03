"""The user's grammatical gender for drafts in the control chat (issue #46).

``users.grammatical_gender`` (``masculine`` / ``feminine`` / NULL) is the form drafts use for
the user in languages with grammatical gender; drafting treats it as a hard constraint
(``DraftRequest.user``). **Only the user sets it**: every user is asked once with two buttons
(``setgender:<masculine|feminine>``), and /settings changes it. Until it is answered the value
stays NULL and drafts behave as before.

The question may carry a suggestion: a local guess from the user's own hand-written messages
(``style/grammatical_gender.py``). With a clear signal the suggested answer is shown first and
marked "· suggested", and a line under the question says which form the messages suggest; with
no signal the question is plain. The guess is never stored.

When: right after setup (``setup_flow._import_and_finish``, over the messages just imported; the
switch to ``active`` claims the user in the same transaction, so the job never races it), and
for users set up before this feature by the ``grammatical_gender_prompt`` job
(:class:`GenderPrompter`, every :data:`PROMPT_SCHEDULE`, due at once at start) over the hand-written
messages still stored (the raw import of setup is not kept, FR-035).

Both go through :func:`ask_once`:

1. The claim first (the cheap check): a conditional ``UPDATE … RETURNING`` sets
   ``users.grammatical_gender_prompt_not_before`` to now + :data:`CLAIM_LEASE` for a due user
   (one sender only; a crash leaves it to expire). Then the stored messages are read and the
   guess made; a failure there releases the claim.
2. Right before the request one conditional statement checks the claim is still ours and
   live, and the user still past setup, not suspended, not asked and without a value: a user
   who chose in /settings meanwhile gets nothing (``chosen``), and neither does one whose claim
   expired or who is no longer due (``lost``); the claim is given back if it is still ours.
3. The question is sent like release notes (``release_notes/sender.py``), with the **same**
   :class:`Pacer` as the release notes job, so both together stay within one rate budget
   (setup's single reply is not paced, like every other reply in a conversation).
   Errors are classified by its :func:`classify`. The job runs one at a time per process
   (:class:`GenderPrompter`).
4. Outcomes:

   - accepted: ``grammatical_gender_prompted_at`` ("asked") is stored and the claim cleared,
     shielded from cancellation and retried a few times; if that still fails, the claim stays
     until the lease expires and the user is asked again rather than marked without being asked;
   - cancelled during the request (in doubt, it may have arrived): marked as asked, like a
     release-notes delivery in doubt, so nobody gets the question twice;
   - network, server and other retryable errors: not asked; retried with the release notes
     :func:`backoff` (``grammatical_gender_prompt_attempts``), and the job run stops so that a
     service started before the network is up does not try everyone at once (an error after
     Telegram accepted the request may rarely mean a second question: accepted over never
     asking);
   - ``TelegramRetryAfter`` (flood wait): not asked; the claim is handed back with
     ``not_before`` at the end of the wait (a sleep could outlast the lease and let another
     run ask too), and the job run stops. Setup waits out one (only one) of up to
     :data:`SETUP_FLOOD_WAIT_MAX_S` inline, so its in-memory suggestion is kept; a longer one
     leaves the user to the job (whose question is then based on the stored messages);
   - if handing the claim back fails, the run still stops and the lease expires;
   - blocked bot or chat gone, or a request Telegram refuses: marked as asked, never retried;
   - a failure before the request (including cancellation): the claim is released.

/settings shows the value (``settings.gender``); 👤 (``settings:gender``) turns the /settings
message into the two choices (``setgender:<value>:settings``) and a press stores the value and
renders /settings again (``settings.py``). A press on the one-off question stores it, ticks it
and confirms. Users still in setup (stale buttons) are refused (``common.not_set_up``).

Personal data: the value is never logged (logs carry ids, counts, outcomes and whether a
suggestion was made), it is part of the user row in the /my_data export, the /my_data summary
shows it and it is deleted with the account.
"""

from __future__ import annotations

import asyncio
import enum
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import ColumnElement, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.clock import Clock, utcnow
from scribe.config import Settings
from scribe.db.models import User, UserState
from scribe.i18n import t
from scribe.logging import get_logger
from scribe.release_notes.sender import (
    RETRY_AFTER_MARGIN_S,
    Outcome,
    Pacer,
    Sleep,
    backoff,
    classify,
)
from scribe.scheduling.scheduler import Every, Scheduler
from scribe.style.grammatical_gender import (
    GRAMMATICAL_GENDERS,
    GrammaticalGender,
    infer_grammatical_gender,
    own_texts,
)
from scribe.telegram.control_bot.render import button, format_callback, keyboard

log = get_logger("scribe.control_bot")

GENDER_ACTION = "setgender"
FROM_SETTINGS = "settings"  # setgender:<value>:settings re-renders /settings
PROMPT_JOB = "grammatical_gender_prompt"
PROMPT_SCHEDULE = Every(timedelta(minutes=5))  # picks up retries after their backoff
PROMPTED_STATES = (UserState.active, UserState.paused, UserState.out_of_office)
CLAIM_LEASE = timedelta(minutes=10)  # a claim older than this was left by a crash
SETUP_FLOOD_WAIT_MAX_S = 60  # setup waits out a flood wait up to this long (keeps its suggestion)
STORE_ATTEMPTS = 3
STORE_RETRY_S = 1.0


class Delivery(enum.StrEnum):
    """How :func:`ask_once` ended (logged as ``result``)."""

    sent = "sent"  # accepted by Telegram; marked as asked
    unknown = "unknown"  # cancelled during the request: in doubt, marked as asked (logged;
    # the cancellation is re-raised)
    retry = "retry"  # a retryable error: not asked, tried again after a backoff
    flood_wait = "flood_wait"  # TelegramRetryAfter: handed back until the wait is over
    chosen = "chosen"  # the user set a value before the request: nothing sent
    lost = "lost"  # the claim expired (or the user is no longer due): nothing sent here
    blocked = "blocked"  # bot blocked or chat gone; marked, never retried
    failed = "failed"  # refused by Telegram; marked, never retried
    skipped = "skipped"  # not claimed: already asked, a value set, or another sender
    released = "released"  # failed before the request; the next run tries again
    not_marked = "not_marked"  # sent, but marking it failed; asked again after the lease


def gender_label(value: str | None, lang: str) -> str:
    return t(f"settings.gender_value.{value or 'unset'}", lang)


def gender_keyboard(
    current: str | None, lang: str, *, from_settings: bool = False
) -> InlineKeyboardMarkup:
    """The two choices for /settings (or after an answer); the current one is ticked."""
    extra = FROM_SETTINGS if from_settings else None
    return keyboard(
        [
            [
                button(
                    ("✅ " if value == current else "") + t(f"btn.gender.{value}", lang),
                    format_callback(GENDER_ACTION, value, extra),
                )
                for value in GRAMMATICAL_GENDERS
            ]
        ]
    )


def question(suggested: GrammaticalGender | None, lang: str) -> tuple[str, InlineKeyboardMarkup]:
    """The one-off question: with a suggestion, it comes first, marked, with a line saying so."""
    text = t("gender.question", lang)
    order: list[GrammaticalGender] = list(GRAMMATICAL_GENDERS)
    if suggested is not None:
        text += "\n" + t(f"gender.suggestion.{suggested}", lang)
        order.sort(key=lambda value: value != suggested)
    buttons = []
    for value in order:
        label = t(f"btn.gender.{value}", lang)
        if value == suggested:
            label = t("btn.gender.suggested", lang, label=label)
        buttons.append(button(label, format_callback(GENDER_ACTION, value)))
    return text, keyboard([buttons])


# --- asking once ---------------------------------------------------------------------------------


def _due(now: datetime) -> list[ColumnElement[bool]]:
    """Not asked, no value, no live claim and no pending backoff."""
    return [
        User.grammatical_gender.is_(None),
        User.grammatical_gender_prompted_at.is_(None),
        or_(
            User.grammatical_gender_prompt_not_before.is_(None),
            User.grammatical_gender_prompt_not_before <= now,
        ),
    ]


async def claim_in(session: AsyncSession, user_id: uuid.UUID, now: datetime) -> datetime | None:
    """The claim, in the caller's transaction: a conditional ``UPDATE … RETURNING`` that sets the
    lease only for a user past setup who is due. Returns the token (the stored ``not_before``),
    or None when not due. Setup calls it in the transaction that switches the user to
    ``active``, so the job can never claim the user in between."""
    lease = now + CLAIM_LEASE
    result = await session.execute(
        update(User)
        .where(
            User.id == user_id,
            User.state.in_(PROMPTED_STATES),
            User.access_suspended_at.is_(None),
            *_due(now),
        )
        .values(grammatical_gender_prompt_not_before=lease)
        .returning(User.grammatical_gender_prompt_not_before)
    )
    return result.scalar_one_or_none()


async def claim(
    session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID, now: datetime
) -> datetime | None:
    async with session_factory() as session:
        token = await claim_in(session, user_id, now)
        await session.commit()
    return token


async def _release(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    token: datetime,
    *,
    retry_at: datetime | None = None,
    count_attempt: bool = False,
) -> None:
    """Give the claim back: due at once, or at ``retry_at`` (a backoff or a flood wait)."""
    values: dict[str, object] = {"grammatical_gender_prompt_not_before": retry_at}
    if count_attempt:
        values["grammatical_gender_prompt_attempts"] = User.grammatical_gender_prompt_attempts + 1
    async with session_factory() as session:
        await session.execute(
            update(User)
            .where(User.id == user_id, User.grammatical_gender_prompt_not_before == token)
            .values(**values)
        )
        await session.commit()


async def _mark_asked(
    session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID, now: datetime
) -> None:
    async with session_factory() as session:
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(
                grammatical_gender_prompted_at=func.coalesce(
                    User.grammatical_gender_prompted_at, now
                ),
                grammatical_gender_prompt_not_before=None,
            )
        )
        await session.commit()


async def _mark(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    clock: Clock,
    sleep: Sleep,
) -> bool:
    """:func:`_mark_asked`, retried; False when every attempt failed (the claim then stays until
    the lease expires, and the user is asked again)."""
    for attempt in range(1, STORE_ATTEMPTS + 1):
        try:
            await _mark_asked(session_factory, user_id, clock())
            return True
        except Exception as exc:
            log.warning(
                "grammatical_gender_mark_failed",
                user_id=str(user_id),
                attempt=attempt,
                error=type(exc).__name__,
            )
            if attempt < STORE_ATTEMPTS:
                await sleep(STORE_RETRY_S * attempt)
    return False


async def _attempts(session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID) -> int:
    async with session_factory() as session:
        value = await session.scalar(
            select(User.grammatical_gender_prompt_attempts).where(User.id == user_id)
        )
    return int(value or 0)


async def _chat_id(
    session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> int | None:
    """The user's control chat id, to pace the request before the claim is checked."""
    async with session_factory() as session:
        return await session.scalar(select(User.telegram_control_chat_id).where(User.id == user_id))


def _log(user_id: uuid.UUID, source: str, suggested: bool, delivery: Delivery) -> None:
    log.info(
        "grammatical_gender_prompt",
        user_id=str(user_id),
        source=source,
        suggested=suggested,
        result=delivery.value,
    )


async def _still_ours(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    token: datetime,
    now: datetime,
) -> tuple[int, str] | None:
    """Right before the request, one conditional statement: (chat id, language) only while the
    claim is ours and live, and the user is still past setup, not suspended, not asked and
    without a value (the conditions of :func:`claim_in`)."""
    async with session_factory() as session:
        result = await session.execute(
            update(User)
            .where(
                User.id == user_id,
                User.grammatical_gender_prompt_not_before == token,
                User.grammatical_gender_prompt_not_before > now,
                User.state.in_(PROMPTED_STATES),
                User.access_suspended_at.is_(None),
                User.grammatical_gender_prompted_at.is_(None),
                User.grammatical_gender.is_(None),
            )
            .values(grammatical_gender_prompt_not_before=token)  # no change; locks the check
            .returning(User.telegram_control_chat_id, User.language)
        )
        row = result.first()
        await session.commit()
    return None if row is None else (row[0], row[1])


async def _value_set(session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID) -> bool:
    async with session_factory() as session:
        value = await session.scalar(select(User.grammatical_gender).where(User.id == user_id))
    return value is not None


async def ask_once(
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    user_id: uuid.UUID,
    suggested: GrammaticalGender | None,
    token: datetime,
    *,
    pacer: Pacer | None,
    clock: Clock = utcnow,
    sleep: Sleep = asyncio.sleep,
    source: str,
    inline_flood_wait_s: float = 0,
) -> Delivery:
    """Ask the user once (module docstring) on the claim ``token`` (:func:`claim`, or setup's
    :func:`claim_in`). ``inline_flood_wait_s``: one flood wait up to this long is waited out here
    (setup, so its in-memory suggestion is not lost); a longer one hands the user back."""
    in_request = False
    delivery = Delivery.released
    flood_until: datetime | None = None
    try:
        pace_chat = await _chat_id(session_factory, user_id)
        if pace_chat is None:
            return Delivery.skipped
        waited_inline = False
        while True:
            if pacer is not None:
                await pacer.wait(pace_chat)
            ours = await _still_ours(session_factory, user_id, token, clock())
            if ours is None:  # chose meanwhile; or the claim expired, or the user is not due
                delivery = (
                    Delivery.chosen if await _value_set(session_factory, user_id) else Delivery.lost
                )
                await _release(session_factory, user_id, token)  # a no-op unless still ours
                _log(user_id, source, suggested is not None, delivery)
                return delivery
            chat_id, lang = ours
            text, markup = question(suggested, lang)
            in_request = True
            try:
                await bot.send_message(chat_id, text, reply_markup=markup)
                delivery = Delivery.sent
            except TelegramRetryAfter as exc:
                in_request = False
                log.warning("grammatical_gender_retry_after", retry_after=exc.retry_after)
                if not waited_inline and exc.retry_after <= inline_flood_wait_s:
                    waited_inline = True  # once only, well within the lease
                    await sleep(exc.retry_after + RETRY_AFTER_MARGIN_S)
                    continue
                # Longer: hand the user back until it is over (a sleep could outlast the lease
                # and let another run ask too); the job run stops.
                flood_until = clock() + timedelta(seconds=exc.retry_after + RETRY_AFTER_MARGIN_S)
                delivery = Delivery.flood_wait
            except Exception as exc:
                in_request = False
                outcome = classify(exc)
                if outcome is Outcome.blocked:
                    delivery = Delivery.blocked
                elif outcome is Outcome.failed:
                    delivery = Delivery.failed
                else:
                    delivery = Delivery.retry
                log.info("grammatical_gender_send_error", error=type(exc).__name__)
            break
    except BaseException:
        # Cancelled or crashed. During the request: in doubt, so asked; before it: released.
        if in_request:
            await asyncio.shield(_mark(session_factory, user_id, clock, sleep))
            _log(user_id, source, suggested is not None, Delivery.unknown)
        else:
            await asyncio.shield(_release(session_factory, user_id, token))
        raise
    if delivery in (Delivery.flood_wait, Delivery.retry):
        try:
            if flood_until is not None:
                await _release(session_factory, user_id, token, retry_at=flood_until)
            else:
                attempts = await _attempts(session_factory, user_id) + 1
                await _release(
                    session_factory,
                    user_id,
                    token,
                    retry_at=clock() + backoff(attempts),
                    count_attempt=True,
                )
        except Exception as exc:  # the claim then expires with its lease; the run still stops
            log.error("grammatical_gender_hand_back_failed", error=type(exc).__name__)
    elif not await asyncio.shield(_mark(session_factory, user_id, clock, sleep)):
        delivery = Delivery.not_marked
    _log(user_id, source, suggested is not None, delivery)
    return delivery


# --- choosing ----------------------------------------------------------------------------------


async def choose(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    value: GrammaticalGender,
    now: datetime,
) -> User | None:
    """Store the user's choice (and that they were asked). None for a user still in setup."""
    async with session_factory() as session:
        result = await session.execute(
            update(User)
            .where(User.id == user_id, User.state.in_(PROMPTED_STATES))
            .values(
                grammatical_gender=value,
                grammatical_gender_prompted_at=func.coalesce(
                    User.grammatical_gender_prompted_at, now
                ),
            )
            .returning(User)
        )
        stored = result.scalars().first()
        await session.commit()
    if stored is not None:
        log.info("settings_changed", user_id=str(user_id), fields=["grammatical_gender"])
    return stored


# --- setup and users set up before this feature -------------------------------------------------


async def after_setup(
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    user_id: uuid.UUID,
    texts: Sequence[str],
    token: datetime,
    *,
    clock: Clock = utcnow,
    sleep: Sleep = asyncio.sleep,
) -> Delivery:
    """Setup: ask with a suggestion from the messages just imported (in memory only), on the
    claim taken with the switch to ``active``. Never raises: a failure leaves it to the job."""
    try:
        suggested = infer_grammatical_gender(texts).value
        return await ask_once(
            session_factory,
            bot,
            user_id,
            suggested,
            token,
            pacer=None,
            clock=clock,
            sleep=sleep,
            source="setup",
            inline_flood_wait_s=SETUP_FLOOD_WAIT_MAX_S,
        )
    except Exception as exc:
        log.error("grammatical_gender_prompt_error", user_id=str(user_id), error=type(exc).__name__)
        return Delivery.released


async def prompt_user(
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    user_id: uuid.UUID,
    *,
    pacer: Pacer,
    clock: Clock = utcnow,
    sleep: Sleep = asyncio.sleep,
) -> Delivery:
    """A user set up before this feature: claimed first (the cheap check), then a suggestion
    from the stored hand-written messages, then the question. A failure while reading
    releases the claim."""
    token = await claim(session_factory, user_id, clock())
    if token is None:
        return Delivery.skipped
    try:
        async with session_factory() as session:
            texts = await own_texts(session, user_id)
    except BaseException:
        await asyncio.shield(_release(session_factory, user_id, token))
        raise
    inference = infer_grammatical_gender(texts)
    log.info(
        "grammatical_gender_inferred",
        user_id=str(user_id),
        texts=len(texts),
        messages=inference.masculine + inference.feminine,
        found=inference.value is not None,
    )
    return await ask_once(
        session_factory,
        bot,
        user_id,
        inference.value,
        token,
        pacer=pacer,
        clock=clock,
        sleep=sleep,
        source="stored",
    )


class GenderPrompter:
    """The ``grammatical_gender_prompt`` job: one run at a time per process (a run that finds
    another in progress returns at once, like ``ReleaseNotesSender.drain``)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        bot: Bot,
        pacer: Pacer,
        *,
        clock: Clock = utcnow,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._session_factory = session_factory
        self._bot = bot
        self._pacer = pacer
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()

    async def run(self) -> int:
        """Every user past setup, with access, not asked, with no value and due; one at a
        time. Stops at the first retryable error or flood wait (the rest stay due). Returns
        how many were asked, 0 when another run is in progress."""
        if self._lock.locked():
            return 0
        async with self._lock:
            return await self._run()

    async def _run(self) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                select(User.id)
                .where(
                    User.state.in_(PROMPTED_STATES),
                    User.access_suspended_at.is_(None),
                    *_due(self._clock()),
                )
                .order_by(User.created_at, User.id)
            )
            user_ids = list(result.scalars().all())
        asked = 0
        for user_id in user_ids:
            try:
                delivery = await prompt_user(
                    self._session_factory,
                    self._bot,
                    user_id,
                    pacer=self._pacer,
                    clock=self._clock,
                    sleep=self._sleep,
                )
            except Exception as exc:  # one user's failure never stops the others
                log.error(
                    "grammatical_gender_prompt_error",
                    user_id=str(user_id),
                    error=type(exc).__name__,
                )
                continue
            asked += delivery is Delivery.sent
            if delivery in (Delivery.retry, Delivery.flood_wait):
                break
        log.info("grammatical_gender_prompt_finished", users=len(user_ids), asked=asked)
        return asked


def register_gender_prompt_job(
    scheduler: Scheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    pacer: Pacer,
    clock: Clock = utcnow,
) -> GenderPrompter:
    """``pacer``: the release notes sender's, so both stay within one rate budget."""
    prompter = GenderPrompter(session_factory, bot, pacer, clock=clock)

    async def job() -> None:
        await prompter.run()

    scheduler.register_job(PROMPT_JOB, PROMPT_SCHEDULE, job, run_at_start=True)
    return prompter


def new_pacer(settings: Settings, clock: Clock, sleep: Sleep = asyncio.sleep) -> Pacer:
    """A pacer at the release notes rate (tests; production shares the release notes one)."""
    return Pacer(settings.release_notes_rate_per_s, clock=clock, sleep=sleep)


__all__ = [
    "CLAIM_LEASE",
    "FROM_SETTINGS",
    "GENDER_ACTION",
    "PROMPT_JOB",
    "Delivery",
    "GenderPrompter",
    "after_setup",
    "ask_once",
    "choose",
    "claim",
    "claim_in",
    "gender_keyboard",
    "gender_label",
    "new_pacer",
    "prompt_user",
    "question",
    "register_gender_prompt_job",
]
