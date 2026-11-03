"""✏️ Edit without retyping, end to end (issue #7): the prompt carries the draft (input-field or
copy button for short drafts, a code block with ForceReply for long ones), and the saved edit
never contains the ``@<bot>`` prefix the input-field button leaves."""

from __future__ import annotations

from aiogram.methods import AnswerInlineQuery, SendMessage
from aiogram.types import ForceReply, InlineKeyboardMarkup, InlineQuery, Update
from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import (
    ActivityKind,
    ActivityRecord,
    Contact,
    Draft,
    DraftState,
    User,
    UserState,
    WorkChat,
)
from scribe.db.repos import drafts as drafts_repo
from scribe.i18n import t
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.registry import LLMRouter
from scribe.telegram.control_bot.edit_prefill import (
    PREFILL_MAX_CHARS,
)
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FrozenClock, MakeChat, MakeContact, MakeUser
from tests.integration.bot_support import callback_update, text_update
from tests.integration.us1_support import US1Harness, grounded_reply, select_all_history

SHORT = "Привет! Отчёт будет к пятнице, пришлю."
LONG = "Привет! Отчёт по Q3 почти готов, осталось сверить цифры. " * 8
BOT = "scribe_test_bot"  # the offline Bot API's getMe username


def _harness(
    reply: str,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
) -> US1Harness:
    fake_llm.set_response("retrieve.filter_context", select_all_history)
    fake_llm.set_response("compose.draft", grounded_reply(reply))
    return US1Harness(session_factory, settings, llm_router, fake_llm, gateway, clock)


async def _drafted(
    harness: US1Harness, make_user: MakeUser, make_chat: MakeChat, make_contact: MakeContact
) -> tuple[User, WorkChat, Contact, Draft]:
    user = await make_user(state=UserState.active)
    contact = await make_contact(user, display_name="Synthetic Anna")
    chat = await make_chat(user, title="Synthetic Anna", telegram_chat_id=contact.telegram_peer_id)
    await harness.incoming(user, chat, contact, "Когда будет отчёт?")
    await harness.tick()
    (draft,) = await harness.drafts(user)
    return user, chat, contact, draft


def _prompt(harness: US1Harness) -> SendMessage:
    return harness.api.of_type(SendMessage)[-1]


async def _stored(
    session_factory: async_sessionmaker[AsyncSession], user: User, draft: Draft
) -> Draft:
    async with session_factory() as session:
        stored = await drafts_repo.get_draft(session, user.id, draft.id)
    assert stored is not None
    return stored


async def test_short_draft_input_field_and_prefix_stripped(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    inline = settings.model_copy(update={"edit_prefill": "inline"})
    harness = _harness(SHORT, session_factory, inline, llm_router, fake_llm, gateway, clock)
    user, chat, _, draft = await _drafted(harness, make_user, make_chat, make_contact)
    control = user.telegram_control_chat_id

    await harness.feed(callback_update(control, f"edit:{draft.id}"))
    prompt = _prompt(harness)
    assert isinstance(prompt.reply_markup, InlineKeyboardMarkup)
    ((button,),) = prompt.reply_markup.inline_keyboard
    assert button.switch_inline_query_current_chat == SHORT
    assert prompt.reply_to_message_id == draft.control_message_id

    # The client puts "@<bot> <draft>" into the input field; typing it shows inline results.
    await harness.feed(
        Update(
            update_id=900001,
            inline_query=InlineQuery(
                id="iq-1",
                from_user=TgUser(id=control, is_bot=False, first_name="Synthetic"),
                query=SHORT,
                offset="",
            ),
        )
    )
    (answer,) = harness.api.of_type(AnswerInlineQuery)
    assert answer.results == [] and answer.is_personal

    edited = "Привет! Отчёт будет в пятницу утром."
    await harness.feed(text_update(control, f"@{BOT} {edited}"))

    (sent,) = gateway.send_text_calls
    assert (sent.chat_id, sent.text) == (chat.telegram_chat_id, edited)
    stored = await _stored(session_factory, user, draft)
    assert stored.state == DraftState.sent and stored.edited_text == edited
    assert stored.text == SHORT


async def test_short_draft_copy_button_without_inline_mode(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    # "auto": the offline getMe reports no inline mode, so the draft is offered for copying.
    harness = _harness(SHORT, session_factory, settings, llm_router, fake_llm, gateway, clock)
    user, _, _, draft = await _drafted(harness, make_user, make_chat, make_contact)
    control = user.telegram_control_chat_id

    await harness.feed(callback_update(control, f"edit:{draft.id}"))
    prompt = _prompt(harness)
    assert isinstance(prompt.reply_markup, InlineKeyboardMarkup)
    ((button,),) = prompt.reply_markup.inline_keyboard
    assert button.copy_text is not None and button.copy_text.text == SHORT
    assert button.switch_inline_query_current_chat is None

    # A "@<bot>"-only message is no edit: nothing is sent and editing goes on.
    await harness.feed(text_update(control, f"@{BOT} "))
    assert gateway.send_text_calls == []
    await harness.feed(text_update(control, "Отчёт будет в пятницу."))
    (sent,) = gateway.send_text_calls
    assert sent.text == "Отчёт будет в пятницу."


async def test_long_draft_code_block_with_force_reply(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
    make_user: MakeUser,
    make_chat: MakeChat,
    make_contact: MakeContact,
) -> None:
    assert len(LONG) > PREFILL_MAX_CHARS
    inline = settings.model_copy(update={"edit_prefill": "inline"})
    harness = _harness(LONG, session_factory, inline, llm_router, fake_llm, gateway, clock)
    user, _, _, draft = await _drafted(harness, make_user, make_chat, make_contact)
    control = user.telegram_control_chat_id

    await harness.feed(callback_update(control, f"edit:{draft.id}"))
    prompt = _prompt(harness)
    assert isinstance(prompt.reply_markup, ForceReply)
    assert prompt.reply_markup.input_field_placeholder == "Your version…"
    assert prompt.parse_mode == "HTML"
    assert prompt.text == f"{t('edit.long_prompt')}\n\n<code>{LONG}</code>"

    await harness.feed(text_update(control, "/cancel"))
    assert gateway.send_text_calls == []
    await harness.feed(callback_update(control, f"edit:{draft.id}"))
    edited = LONG.replace("Q3", "третьему кварталу")
    await harness.feed(text_update(control, edited))

    (sent,) = gateway.send_text_calls
    assert sent.text == edited
    stored = await _stored(session_factory, user, draft)
    assert stored.state == DraftState.sent and stored.edited_text == edited
    assert ActivityKind.edited_and_approved in await _activity_kinds(session_factory, user)


async def _activity_kinds(
    session_factory: async_sessionmaker[AsyncSession], user: User
) -> list[ActivityKind]:
    async with session_factory() as session:
        result = await session.execute(
            select(ActivityRecord.kind).where(ActivityRecord.user_id == user.id)
        )
        return list(result.scalars().all())
