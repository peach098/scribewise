"""Russian rendering of the US3/US4 control-bot texts (issue #8 on top of /tasks, /commitments,
/chats overrides, /ooo, /history, /my_data and /profile)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from scribe.db.models import (
    ActivityKind,
    ActivityRecord,
    ChatKind,
    Commitment,
    ConnectionStatus,
    TaskProvider,
    TaskSourceConnection,
    User,
    WorkChat,
)
from scribe.i18n import format_weekday_date, t
from scribe.retention.user_data import DataSummary, TableSummary
from scribe.telegram.control_bot.account_command import summary_text
from scribe.telegram.control_bot.commitments_command import commitment_text
from scribe.telegram.control_bot.settings import (
    chat_settings_keyboard,
    chat_settings_text,
    history_line,
)
from scribe.telegram.control_bot.tasks_command import status_keyboard, status_text

NOW = datetime(2026, 11, 5, 11, 5, tzinfo=UTC)


def test_weekday_dates() -> None:
    assert format_weekday_date(date(2026, 11, 5), "en") == "Thu 5 Nov"
    assert format_weekday_date(date(2026, 11, 5), "ru") == "Чт, 5 ноя"


def test_russian_task_source_status() -> None:
    conn = TaskSourceConnection(
        provider=TaskProvider.trello,
        status=ConnectionStatus.active,
        config_json={"board_ids": ["a", "b"]},
    )
    assert status_text(conn, "ru") == (
        "🗂 Таск-трекер\n✅ Trello: подключён (только чтение)\n📋 Доски: выбрано 2"
    )
    assert "выбрана 1" in status_text(
        TaskSourceConnection(
            provider=TaskProvider.trello,
            status=ConnectionStatus.active,
            config_json={"board_ids": ["a"]},
        ),
        "ru",
    )
    buttons = [b.text for row in status_keyboard(None, "ru").inline_keyboard for b in row]
    assert buttons == ["🔌 Подключить Trello", "🔌 Подключить Jira"]


def test_russian_commitment() -> None:
    commitment = Commitment(what="прислать отчёт", due=date(2026, 11, 5), source_message_text=None)
    assert commitment_text(commitment, "Анна", "ru") == "🤝 прислать отчёт · Анна · до Чт, 5 ноя"
    assert commitment_text(commitment, "Anna", "en") == "🤝 прислать отчёт · Anna · due Thu 5 Nov"
    undated = Commitment(what="позвонить", due=None, source_message_text=None)
    assert commitment_text(undated, "", "ru") == "🤝 позвонить · кому-то · без срока"


def test_russian_chat_settings() -> None:
    user = User(language="ru", delay_min_s=120, delay_max_s=600, skip_closing=True)
    chat = WorkChat(
        id=uuid.uuid4(),
        title="Финансы",
        kind=ChatKind.group,
        enabled=True,
        override_language=None,
        override_formality=4,
        override_delay_min_s=None,
        override_delay_max_s=None,
    )
    text = chat_settings_text(chat, user)
    assert text.splitlines() == [
        "💬 Финансы",
        "⏱ Задержка чтения: общая (2 мин–10 мин, случайно для каждого сообщения)",
        "🌐 Язык: как у входящего сообщения",
        "🎩 Формальность: 4/5 (1 — неформально … 5 — официально)",
        "👥 Черновики в этой группе: только когда вас отметили или ответили на ваше сообщение",
        "💤 Пропуск «спасибо/пока»: Общая (ВКЛ)",
        "▶️ Включён: сообщения читаются, черновики готовятся",
    ]
    keyboard = chat_settings_keyboard(chat, "ru").inline_keyboard
    buttons = [b.text for row in keyboard for b in row]
    assert "⏸ Выключить чат" in buttons
    assert {"✓ Общая (ВКЛ)", "Всегда черновик", "Пропускать"} <= set(buttons)  # 004
    # 006: the group-mode choice is gone.
    assert not {"👥 Только упоминания", "👥 Каждое сообщение"} & set(buttons)
    assert not [
        b for row in keyboard for b in row if (b.callback_data or "").startswith("chatgroup")
    ]


def test_english_group_chat_settings_state_the_rule() -> None:
    user = User(language="en", delay_min_s=120, delay_max_s=600, skip_closing=True)
    chat = WorkChat(
        id=uuid.uuid4(),
        title="Finance",
        kind=ChatKind.group,
        enabled=True,
        override_language=None,
        override_formality=None,
        override_delay_min_s=None,
        override_delay_max_s=None,
    )
    lines = chat_settings_text(chat, user).splitlines()
    assert "👥 Drafts in this group: only when you are tagged or replied to" in lines
    keyboard = chat_settings_keyboard(chat, "en").inline_keyboard
    assert not [
        b for row in keyboard for b in row if (b.callback_data or "").startswith("chatgroup")
    ]
    assert not {"👥 Mentions only", "👥 Every message"} & {b.text for row in keyboard for b in row}


def test_russian_history_line() -> None:
    record = ActivityRecord(kind=ActivityKind.edited_and_approved, at=NOW, meta_json={})
    assert history_line(record, "Анна", "Europe/Moscow", "ru") == (
        "Чт, 5 ноя, 14:05 · ✏️ Исправлено и одобрено вами · Анна"
    )
    assert (
        history_line(record, None, "UTC", "en") == "Thu 5 Nov 11:05 · ✏️ Edited and approved by you"
    )


def test_russian_data_summary() -> None:
    summary = DataSummary(
        messages=TableSummary(3, oldest=NOW, next_removal=NOW),
        drafts=TableSummary(1),
        profiles=TableSummary(2),
        profiles_by_state={"current": 2},
        proposals=0,
        commitments_open=1,
        commitments=TableSummary(0),
        activity=TableSummary(4),
        contacts=2,
        work_chats=1,
        telegram_status=None,
        task_source=None,
    )
    text = summary_text(summary, "UTC", 21, "ru")
    assert text.startswith("📦 Что я храню о вас\n💬 Сообщения: 3 (самое старое — 5 ноя 2026; ")
    assert "каждое удаляется через 21 день после отправки" in text
    assert "🤝 Обещания: открытых — 1, закрытых — 0" in text
    assert "🔌 Сеанс Telegram: не подключён" in text
    assert t("delete.ask", "ru", word="DELETE").endswith(
        "Чтобы подтвердить, напишите DELETE, или отправьте /cancel."
    )


def test_history_lines_for_skipped_messages() -> None:
    """004 T035: skipped (with its reason) and "Draft anyway", never content."""
    skipped = ActivityRecord(
        kind=ActivityKind.skipped,
        at=NOW,
        meta_json={"category": "thanks", "source": "model", "confidence": 0.95},
    )
    assert history_line(skipped, "Анна", "Europe/Moscow", "ru") == (
        "Чт, 5 ноя, 14:05 · 💤 Пропущено — благодарность · Анна"
    )
    assert history_line(skipped, None, "UTC", "en") == "Thu 5 Nov 11:05 · 💤 Skipped — thanks"
    anyway = ActivityRecord(kind=ActivityKind.draft_anyway, at=NOW, meta_json={})
    assert history_line(anyway, None, "UTC", "en") == "Thu 5 Nov 11:05 · ✍️ Drafted on request"
    assert history_line(anyway, None, "UTC", "ru").endswith("✍️ Черновик по запросу")
    bare = ActivityRecord(kind=ActivityKind.skipped, at=NOW, meta_json={})
    assert history_line(bare, None, "UTC", "en").endswith("· 💤 Skipped")
