"""Offline aiogram helpers: a Bot API session that records requests instead of calling
Telegram, and update builders. Synthetic data only."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetMe, GetUpdates, SendMessage, SendPhoto, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User

_update_ids = itertools.count(1)
_message_ids = itertools.count(1)
_callback_ids = itertools.count(1)
NOW = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
TEST_TOKEN = "123456:TEST-TOKEN-NOT-REAL"


class RecordingSession(BaseSession):
    """Records every request. ``outcomes`` is a queue for ``SendMessage`` calls: each call takes
    the next item; an exception instance is raised (the request goes to ``failed`` instead of
    ``requests``), ``None`` or an empty queue means success."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self.failed: list[TelegramMethod[Any]] = []
        self.outcomes: list[BaseException | None] = []
        self.photo_message_ids: list[int] = []  # ids of the messages sent with SendPhoto

    @property
    def sent_texts(self) -> list[str]:
        return [r.text for r in self.requests if isinstance(r, SendMessage)]

    def of_type[M: TelegramMethod[Any]](self, method: type[M]) -> list[M]:
        return [r for r in self.requests if isinstance(r, method)]

    def clear(self) -> None:
        self.requests.clear()

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109 - aiogram BaseSession signature
    ) -> Any:
        if isinstance(method, SendMessage) and self.outcomes:
            outcome = self.outcomes.pop(0)
            if outcome is not None:
                self.failed.append(method)
                raise outcome
        self.requests.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=next(_message_ids),
                date=NOW,
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
            )
        if isinstance(method, SendPhoto):
            message_id = next(_message_ids)
            self.photo_message_ids.append(message_id)
            return Message(
                message_id=message_id,
                date=NOW,
                chat=Chat(id=int(method.chat_id), type="private"),
                caption=method.caption,
            )
        if isinstance(method, GetUpdates):
            await asyncio.sleep(0.05)  # long-polling stand-in: no updates
            return []
        if isinstance(method, GetMe):
            return User(id=123456, is_bot=True, first_name="Scribe", username="scribe_test_bot")
        return True

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109 - aiogram BaseSession signature
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        yield b""

    async def close(self) -> None:
        return None


def offline_bot() -> tuple[Bot, RecordingSession]:
    session = RecordingSession()
    return Bot(token=TEST_TOKEN, session=session), session


def text_update(
    chat_id: int,
    text: str,
    *,
    chat_type: str = "private",
    reply_to: int | None = None,
    language_code: str | None = None,
    username: str | None = None,
) -> Update:
    """A text message; ``reply_to`` makes it a reply to that message id of the same chat;
    ``language_code`` is the sender's Telegram language, ``username`` their @username."""
    chat = Chat(id=chat_id, type=chat_type)
    replied = (
        Message(message_id=reply_to, date=NOW, chat=chat, text="synthetic card")
        if reply_to is not None
        else None
    )
    return Update(
        update_id=next(_update_ids),
        message=Message(
            message_id=next(_message_ids),
            date=NOW,
            chat=chat,
            from_user=User(
                id=chat_id,
                is_bot=False,
                first_name="Synthetic",
                last_name="User",
                language_code=language_code,
                username=username,
            ),
            text=text,
            reply_to_message=replied,
        ),
    )


def callback_update(
    chat_id: int,
    data: str,
    *,
    message_id: int = 1,
    from_user_id: int | None = None,
    with_message: bool = True,
    language_code: str | None = None,
    message_text: str = "synthetic card",
    username: str | None = None,
) -> Update:
    """An inline-button press in the private control chat ``chat_id`` (by its owner unless
    ``from_user_id`` says otherwise; without a message, like an inline-mode button) on a
    message showing ``message_text``."""
    message = Message(
        message_id=message_id,
        date=NOW,
        chat=Chat(id=chat_id, type="private"),
        text=message_text,
    )
    return Update(
        update_id=next(_update_ids),
        callback_query=CallbackQuery(
            id=str(next(_callback_ids)),
            from_user=User(
                id=from_user_id or chat_id,
                is_bot=False,
                first_name="Synthetic",
                language_code=language_code,
                username=username,
            ),
            chat_instance="synthetic",
            data=data,
            message=message if with_message else None,
        ),
    )
