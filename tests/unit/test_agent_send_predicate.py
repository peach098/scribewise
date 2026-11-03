"""``drafts_repo.agent_send``: the one "sent through the agent" predicate (issue #46 review).
Correlated to the outer message, never a cross join with ``messages``."""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.dialects import postgresql

from scribe.db.models import Message
from scribe.db.repos import drafts as drafts_repo

USER = uuid.uuid4()
CHAT = uuid.uuid4()


def _sql(statement: Select[Any]) -> str:
    compiled = statement.compile(dialect=postgresql.dialect())  # type: ignore[no-untyped-call]
    return " ".join(str(compiled).split())


def _from_clauses(sql: str) -> list[str]:
    return re.findall(r"FROM (.*?)(?: WHERE| JOIN|\)|$)", sql)


def test_correlated_to_the_outer_message() -> None:
    """The gender inference: chat and message id are the outer row's columns."""
    sql = _sql(
        select(Message.text).where(
            ~drafts_repo.agent_send(USER, Message.work_chat_id, Message.telegram_message_id)
        )
    )
    assert "conversation_turns, messages" not in sql and "messages, conversation_turns" not in sql
    froms = _from_clauses(sql)
    assert froms[0] == "messages"
    assert all("messages" not in clause for clause in froms[1:]), sql
    assert "conversation_turns.work_chat_id = messages.work_chat_id" in sql
    assert "drafts.sent_telegram_message_id = messages.telegram_message_id" in sql


def test_answered_later_shape() -> None:
    """intake.router: a fixed chat, the outer message's Telegram id (as main had it)."""
    sql = _sql(
        select(Message.id).where(~drafts_repo.agent_send(USER, CHAT, Message.telegram_message_id))
    )
    froms = _from_clauses(sql)
    assert froms[0] == "messages"
    assert all("messages" not in clause for clause in froms[1:]), sql
    assert "conversation_turns.work_chat_id = %(work_chat_id_1)s" in sql


def test_is_agent_send_shape() -> None:
    """intake.events: values only, no ``messages`` at all."""
    sql = _sql(select(drafts_repo.agent_send(USER, CHAT, 42)))
    assert "messages" not in sql.replace("sent_telegram_message_id", "")
