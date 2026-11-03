"""The shared chat picker (specs/007 T007): the keyboard is the setup one for every purpose,
and the exclusion filter drops existing work chats. Synthetic data only."""

from __future__ import annotations

from scribe.db.models import ChatKind
from scribe.telegram.control_bot.chat_picker import (
    PAGE_SIZE,
    TITLE_CHARS,
    available_dialogs,
    chat_kind,
    picker_keyboard,
)
from scribe.telegram.gateway import Dialog


def _rows(n: int) -> list[list[object]]:
    return [[-100900000000 - i, f"Chat {i} (synthetic)", "group"] for i in range(n)]


def _datas(markup: object) -> list[list[str]]:
    return [[b.callback_data or "" for b in row] for row in markup.inline_keyboard]  # type: ignore[attr-defined]


def _texts(markup: object) -> list[list[str]]:
    return [[b.text for b in row] for row in markup.inline_keyboard]  # type: ignore[attr-defined]


def test_one_page_rows_marks_and_done_label() -> None:
    rows = _rows(3)
    markup = picker_keyboard(rows, {-100900000001}, 0, "en")
    assert _texts(markup) == [
        ["⬜ Chat 0 (synthetic)"],
        ["✅ Chat 1 (synthetic)"],
        ["⬜ Chat 2 (synthetic)"],
        ["Done (1 selected)"],
    ]
    assert _datas(markup)[0] == ["pickchat:-100900000000"]
    assert _datas(markup)[-1] == ["pickdone:0"]


def test_paging_and_clamping() -> None:
    rows = _rows(PAGE_SIZE * 2 + 1)  # 3 pages
    first = picker_keyboard(rows, set(), 0, "en")
    assert len(first.inline_keyboard) == PAGE_SIZE + 2
    assert _datas(first)[PAGE_SIZE] == ["pickpage:1"]
    middle = picker_keyboard(rows, set(), 1, "en")
    assert _datas(middle)[PAGE_SIZE] == ["pickpage:0", "pickpage:2"]
    assert _texts(middle)[PAGE_SIZE] == ["◀️", "▶️"]
    last = picker_keyboard(rows, set(), 99, "en")  # clamped to the last page
    assert _texts(last)[0] == [f"⬜ Chat {PAGE_SIZE * 2} (synthetic)"]
    assert _datas(last)[1] == ["pickpage:1"]
    assert _texts(picker_keyboard(rows, set(), -5, "en"))[0] == ["⬜ Chat 0 (synthetic)"]


def test_long_titles_are_truncated_and_done_label_is_localized() -> None:
    title = "X" * (TITLE_CHARS + 10)
    markup = picker_keyboard([[1, title, "private"]], {1}, 0, "ru")
    label = _texts(markup)[0][0]
    assert label == "✅ " + "X" * (TITLE_CHARS - 1) + "…"
    assert _texts(markup)[-1] == ["Готово (выбрано: 1)"]


def test_empty_picker_has_only_done() -> None:
    assert _texts(picker_keyboard([], set(), 0, "en")) == [["Done (0 selected)"]]


def test_available_dialogs_drops_existing_and_keeps_order() -> None:
    dialogs = [
        Dialog(chat_id=-100900000001, title="Project X (synthetic)", kind="group"),
        Dialog(chat_id=900000002, title="Anna (synthetic)", kind="private"),
        Dialog(chat_id=-100900000003, title="Old (synthetic)", kind="group"),
        Dialog(chat_id=900000004, title="Petr (synthetic)", kind="private"),
    ]
    # Enabled and disabled work chats alike are just ids here.
    left = available_dialogs(dialogs, {900000002, -100900000003, 123})
    assert [d.chat_id for d in left] == [-100900000001, 900000004]
    assert available_dialogs(dialogs, frozenset()) == dialogs


def test_chat_kind_mapping() -> None:
    assert chat_kind("private") == ChatKind.private
    assert chat_kind("group") == ChatKind.group
    assert chat_kind("channel_comments") == ChatKind.channel_comments
    assert chat_kind("unknown") == ChatKind.group


# --- specs/008 work account mode (T017) -----------------------------------------------------------


def _mixed() -> list[list[object]]:
    return [
        [900000101, "Test Contact A", "private"],
        [900000102, "Test Contact B", "private"],
        [-100900000201, "Team (synthetic)", "group"],
    ]


def test_without_the_switch_the_keyboard_is_unchanged() -> None:
    rows = _mixed()
    assert _texts(picker_keyboard(rows, {900000101}, 0, "en", work_account=None)) == _texts(
        picker_keyboard(rows, {900000101}, 0, "en")
    )


def test_switch_off_shows_the_turn_on_row_and_plain_toggles() -> None:
    markup = picker_keyboard(_mixed(), {-100900000201}, 0, "en", work_account=False)
    assert _datas(markup)[0] == ["pickwa:on"]
    assert _texts(markup)[0] == ["💼 Work account mode: off (tap to turn on)"]
    assert [row[0] for row in _texts(markup)[1:4]] == [
        "⬜ Test Contact A",
        "⬜ Test Contact B",
        "✅ Team (synthetic)",
    ]
    assert _texts(markup)[-1] == ["Done (1 selected)"]


def test_switch_on_marks_private_chats_by_exclusion_and_groups_by_selection() -> None:
    markup = picker_keyboard(
        _mixed(), {-100900000201}, 0, "en", work_account=True, excluded={900000102}
    )
    assert _datas(markup)[0] == ["pickwa:off"]
    assert [row[0] for row in _texts(markup)[1:4]] == [
        "✅ Test Contact A",
        "🚫 Test Contact B",
        "✅ Team (synthetic)",
    ]
    assert _texts(markup)[-1] == ["Done (all private chats; excluded: 1; groups: 1)"]
    ru = picker_keyboard(_mixed(), set(), 0, "ru", work_account=True, excluded={900000102})
    assert _texts(ru)[-1] == ["Готово (все личные чаты; исключено: 1; групп: 0)"]


def test_exclusions_only_picker() -> None:
    rows = [r for r in _mixed() if r[2] == "private"]
    markup = picker_keyboard(rows, set(), 0, "en", excluded={900000101}, exclusions_only=True)
    assert _texts(markup) == [["🚫 Test Contact A"], ["✅ Test Contact B"], ["Done"]]


def test_paging_with_the_switch_and_callback_sizes() -> None:
    rows = [[-1009000000000 - i, f"Chat {i} (synthetic)", "group"] for i in range(PAGE_SIZE + 1)]
    markup = picker_keyboard(rows, set(), 0, "en", work_account=True)
    assert len(markup.inline_keyboard) == PAGE_SIZE + 3  # switch, rows, nav, done
    assert _datas(markup)[PAGE_SIZE + 1] == ["pickpage:1"]
    for row in _datas(markup):
        for data in row:
            assert len(data.encode()) <= 64
