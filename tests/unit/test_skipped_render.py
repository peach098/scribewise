"""``/skipped`` list and per-chat skip settings (004 T021, T028; contracts/control-chat.md)."""

from __future__ import annotations

import uuid

from aiogram.types import InlineKeyboardMarkup

from scribe.telegram.control_bot.render import (
    CALLBACK_DATA_MAX_BYTES,
    ChatSkipView,
    SkippedItemView,
    chat_skip_settings_keyboard,
    is_chat_skip_settings,
    parse_callback,
    render_chat_skip_settings,
    render_skipped_list,
    skipped_keyboard,
)


def _item(n: int = 0, **values: object) -> SkippedItemView:
    base: dict[str, object] = {
        "turn_id": uuid.UUID(int=1000 + n),
        "work_chat_id": uuid.UUID(int=2000 + n),
        "sender": "Anna Testova",
        "chat_title": "Design team",
        "local_time": "Tue 14:02",
        "preview": "thanks, that helps!",
        "reason": "thanks",
        "available": True,
        "chat_override": None,
    }
    base.update(values)
    return SkippedItemView(**base)  # type: ignore[arg-type]


def _callbacks(markup: InlineKeyboardMarkup) -> list[str]:
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert all(d is not None for d in data)
    return [d for d in data if d is not None]


def _assert_valid(markup: InlineKeyboardMarkup) -> None:
    for data in _callbacks(markup):
        parse_callback(data)
        assert len(data.encode("utf-8")) <= CALLBACK_DATA_MAX_BYTES


def test_skipped_list_layout() -> None:
    items = [
        _item(0),
        _item(
            1,
            sender="Ivan Primerov",
            chat_title="Ivan Primerov",
            reason="farewell",
            preview="bye, have a good weekend",
            available=False,
        ),  # fmt: skip
        _item(2, reason="acknowledgment", preview="x" * 200),
    ]
    text = render_skipped_list(items, total=3, page=0, global_on=True, days=7, lang="en")
    lines = text.split("\n")
    assert lines[0].startswith("💤 Skipped in the last 7 days — 3")
    assert lines[0].endswith("Skipping thanks/ok/bye: ON")
    assert lines[1] == "Forwarded messages without the contact's own text are always skipped."
    assert "1. Anna Testova · Design team · Tue 14:02" in lines
    assert "   “thanks, that helps!”" in lines
    assert "   Reason: thanks" in lines
    assert "2. Ivan Primerov · Ivan Primerov · Tue 14:02" in lines
    assert "   Reason: goodbye" in lines and "   — you already replied" in lines
    assert "   Reason: acknowledgment" in lines
    long_preview = next(line for line in lines if line.startswith("   “x"))
    assert len(long_preview) == 3 + 80 + 2 and long_preview.endswith("…”")

    off = render_skipped_list(items, total=3, page=0, global_on=False, days=7, lang="en")
    assert off.split("\n")[0].endswith("Skipping thanks/ok/bye: OFF")
    page2 = render_skipped_list([_item(0)], total=11, page=1, global_on=True, days=7, lang="en")
    assert "11. Anna Testova · Design team · Tue 14:02" in page2.split("\n")


def test_skipped_list_empty() -> None:
    assert (
        render_skipped_list([], total=0, page=0, global_on=True, days=7, lang="en")
        == "Nothing skipped in the last 7 days. Skipping thanks/ok/bye is ON.\n"
        "Forwarded messages without the contact's own text are always skipped."
    )
    off = render_skipped_list([], total=0, page=0, global_on=False, days=7, lang="en")
    assert off.split("\n")[0].endswith("is OFF.")
    markup = skipped_keyboard([], page=0, pages=1, global_on=True, lang="en")
    assert _callbacks(markup) == ["skipcfg:global:off", "skipchats:0"]


def test_skipped_keyboard_buttons() -> None:
    items = [
        _item(0),
        _item(1, available=False),
        _item(2, chat_override=False),
    ]
    markup = skipped_keyboard(items, page=0, pages=1, global_on=True, lang="en")
    _assert_valid(markup)
    data = _callbacks(markup)
    assert data[:2] == ["skipcfg:global:off", "skipchats:0"]
    assert f"draftany:{items[0].turn_id}" in data
    assert f"draftany:{items[1].turn_id}" not in data  # no longer available
    assert f"skipchat:{items[0].work_chat_id}:off" in data
    assert f"skipchat:{items[2].work_chat_id}:inherit" in data
    assert f"skipchat:{items[2].work_chat_id}:off" not in data
    assert not any(d.startswith("skippage:") for d in data)  # ≤ 10 items: no pager

    on = skipped_keyboard(items, page=0, pages=1, global_on=False, lang="en")
    assert _callbacks(on)[0] == "skipcfg:global:on"
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "Turn skipping off" in labels and "⚙️ Per-chat settings" in labels


def test_forwarded_items_have_no_per_chat_skip_button() -> None:
    """#47: a forwards-only turn is skipped whatever the chat's setting: no button for it."""
    items = [_item(0, reason="forwarded"), _item(1, reason="forwarded", chat_override=False)]
    data = _callbacks(skipped_keyboard(items, page=0, pages=1, global_on=True, lang="en"))
    assert f"draftany:{items[0].turn_id}" in data and f"draftany:{items[1].turn_id}" in data
    assert not any(d.startswith("skipchat:") for d in data)
    text = render_skipped_list(items, total=2, page=0, global_on=True, days=7, lang="en")
    assert "   Reason: forwarded message" in text.split("\n")


def test_skipped_pager_only_with_more_than_one_page() -> None:
    items = [_item(i) for i in range(10)]
    first = _callbacks(skipped_keyboard(items, page=0, pages=2, global_on=True, lang="en"))
    assert "skippage:1:10" in first and "skippage:0:10" not in first
    second = _callbacks(skipped_keyboard(items[:1], page=1, pages=2, global_on=True, lang="en"))
    assert "skippage:0:10" in second and "skippage:2:10" not in second


def test_drafting_item() -> None:
    item = _item(0, drafting=True)
    text = render_skipped_list([item], total=1, page=0, global_on=True, days=7, lang="en")
    assert "   ✍️ Drafting…" in text.split("\n")
    data = _callbacks(skipped_keyboard([item], page=0, pages=1, global_on=True, lang="en"))
    assert f"draftany:{item.turn_id}" not in data


# --- per-chat settings (T028) -------------------------------------------------------------------


def _chat(n: int, value: bool | None, title: str = "") -> ChatSkipView:
    return ChatSkipView(
        work_chat_id=uuid.UUID(int=3000 + n), title=title or f"Chat {n}", value=value
    )


def test_chat_skip_settings_layout() -> None:
    chats = [_chat(0, None, "Design team"), _chat(1, False, "Anna Testova"), _chat(2, True)]
    text = render_chat_skip_settings(chats, page=0, pages=1, global_on=True, lang="en")
    lines = text.split("\n")
    assert lines[0].startswith("⚙️ Skip thanks/bye per chat") and lines[0].endswith("Global: ON")
    assert "1. Design team — ✓ Global (ON)" in lines
    assert "2. Anna Testova — ✓ Always draft" in lines
    assert "3. Chat 2 — ✓ Skip" in lines
    off = render_chat_skip_settings(chats, page=0, pages=1, global_on=False, lang="en")
    assert "1. Design team — ✓ Global (OFF)" in off.split("\n")
    assert render_chat_skip_settings([], page=0, pages=1, global_on=True, lang="en").endswith(
        "No enabled work chats."
    )


def test_chat_skip_settings_keyboard() -> None:
    chats = [_chat(0, None), _chat(1, False), _chat(2, True)]
    markup = chat_skip_settings_keyboard(chats, page=0, pages=1, global_on=True, lang="en")
    _assert_valid(markup)
    rows = markup.inline_keyboard
    assert [b.callback_data for b in rows[0]] == ["skipcfg:global:off"]
    by_chat = rows[1:4]
    for chat, row in zip(chats, by_chat, strict=True):
        assert [b.callback_data for b in row] == [
            f"skipchat:{chat.work_chat_id}:inherit",
            f"skipchat:{chat.work_chat_id}:off",
            f"skipchat:{chat.work_chat_id}:on",
        ]
    assert [b.text for b in by_chat[0]] == ["1 · ✓ Global (ON)", "Always draft", "Skip"]
    assert [b.text for b in by_chat[1]] == ["2 · Global (ON)", "✓ Always draft", "Skip"]
    assert [b.text for b in by_chat[2]] == ["3 · Global (ON)", "Always draft", "✓ Skip"]
    assert rows[-1][0].callback_data == "skippage:0:10"  # back to the list
    assert not any((b.callback_data or "").startswith("skipchats:") for row in rows for b in row)


def test_chat_skip_settings_paged() -> None:
    chats = [_chat(i, None) for i in range(10)]
    first = _callbacks(
        chat_skip_settings_keyboard(chats, page=0, pages=2, global_on=True, lang="en")
    )
    assert "skipchats:1" in first and "skipchats:-1" not in first
    second = _callbacks(
        chat_skip_settings_keyboard(chats[:2], page=1, pages=2, global_on=True, lang="en")
    )
    assert "skipchats:0" in second
    text = render_chat_skip_settings(chats[:2], page=1, pages=2, global_on=True, lang="en")
    assert "11. Chat 0 — ✓ Global (ON)" in text.split("\n")


# --- Russian (issue #8 catalogs) ------------------------------------------------------------------


def test_skipped_list_in_russian() -> None:
    text = render_skipped_list([_item(0)], total=1, page=0, global_on=True, days=7, lang="ru")
    lines = text.split("\n")
    assert lines[0] == "💤 Пропущено за последние 7 дней — 1   ·   Пропуск «спасибо/ок/пока»: ВКЛ"
    assert lines[1] == "Пересланные сообщения без текста самого собеседника пропускаются всегда."
    assert "   Причина: благодарность" in lines
    assert render_skipped_list([], total=0, page=0, global_on=False, days=1, lang="ru") == (
        "За последний 1 день ничего не пропущено. Пропуск «спасибо/ок/пока»: ВЫКЛ.\n"
        "Пересланные сообщения без текста самого собеседника пропускаются всегда."
    )
    forwarded = render_skipped_list(
        [_item(0, reason="forwarded")], total=1, page=0, global_on=True, days=7, lang="ru"
    )
    assert "   Причина: пересланное сообщение" in forwarded.split("\n")
    labels = [
        b.text
        for row in skipped_keyboard(
            [_item(0)], page=0, pages=1, global_on=True, lang="ru"
        ).inline_keyboard
        for b in row
    ]
    assert labels[:2] == ["Выключить пропуск", "⚙️ Настройки по чатам"]
    assert "1 · ✍️ Всё равно черновик" in labels


def test_chat_skip_settings_in_russian_are_recognised() -> None:
    text = render_chat_skip_settings([_chat(0, None)], page=0, pages=1, global_on=True, lang="ru")
    assert text.split("\n")[2] == "1. Chat 0 — ✓ Общая (ВКЛ)"
    assert is_chat_skip_settings(text)
    english = render_chat_skip_settings([], page=0, pages=1, global_on=True, lang="en")
    assert is_chat_skip_settings(english)
    assert not is_chat_skip_settings(
        render_skipped_list([_item(0)], total=1, page=0, global_on=True, days=7, lang="ru")
    )
