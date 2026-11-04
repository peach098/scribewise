"""Draft card layout (T038, contracts/control-chat.md "Draft card")."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scribe.drafting.stored_output import StoredEarlier, pack_output
from scribe.telegram.control_bot.render import (
    TELEGRAM_TEXT_MAX,
    DraftCardView,
    armed_draft_keyboard,
    confirm_send_keyboard,
    draft_keyboard,
    parse_callback,
    render_draft_card,
)
from scribe.telegram.control_bot.review import source_labels
from tests.integration.us1_support import draft_output


def _view(**values: object) -> DraftCardView:
    base: dict[str, object] = {
        "contact_title": "Synthetic Anna",
        "chat_kind": "private",
        "read_at": "10:03",
        "incoming": ["Привет!", "Когда будет готов отчёт за Q3?"],
        "profile_label": "global",
        "tone_shift": "none",
        "draft_text": "Привет! Отчёт будет в пятницу.",
        "expires": "10:03 tomorrow",
    }
    base.update(values)
    return DraftCardView(**base)  # type: ignore[arg-type]


def test_minimal_card_layout() -> None:
    card = render_draft_card(_view(sources=["Synthetic Anna 14 Oct “Когда будет готов…”"]))
    lines = card.split("\n")
    assert lines[0] == "💬 Synthetic Anna   ·   private chat   ·   read 10:03"
    assert lines[1] == "› Привет!"
    assert lines[2] == "› Когда будет готов отчёт за Q3?"
    assert "✍️ Draft (global, tone shift: none)" in lines
    assert "Привет! Отчёт будет в пятницу." in lines
    assert "📎 Sources: Synthetic Anna 14 Oct “Когда будет готов…”" in lines
    assert lines[-1] == "⏳ Expires 10:03 tomorrow"
    assert "⚑ Flags" not in card and "❓" not in card


def test_flags_needs_info_warnings_and_status() -> None:
    card = render_draft_card(
        _view(
            tone_shift="more_formal",
            commitment="send the report (by 2026-10-16)",
            conflicts=["Q3 report due Friday"],
            sensitive=True,
            sensitive_categories=["credentials", "personal_data"],
            needs_info=["Which day exactly?"],
            needs_info_flag=True,
            sources=["a", "b", "c", "d", "e"],
            warnings=["weak_style_profile", "filter_unavailable", "unsupported_sources"],
            status="✅ Sent 10:07",
        )
    )
    assert "tone shift: more formal" in card
    assert (
        "⚑ Flags: 🤝 commits you to send the report (by 2026-10-16)  ·  "
        '⚠️ conflicts with "Q3 report due Friday"  ·  🔒 sensitive (credentials, personal data)'
    ) in card
    assert "❓ Needs info:\n1. Which day exactly?" in card
    assert "Reply with the missing" not in card  # finished card: nothing left to fill
    assert "📎 Sources: a · b · c · +2 more" in card
    assert "weak style profile" in card and "context selection failed" in card
    assert "could not verify" in card
    assert card.endswith("\n\n✅ Sent 10:07")


def test_needs_info_questions_are_numbered_with_a_fill_hint() -> None:
    card = render_draft_card(
        _view(needs_info=["Which day?", "What time?"], needs_info_flag=True)
    ).split("\n")
    start = card.index("❓ Needs info:")
    assert card[start + 1 : start + 4] == [
        "1. Which day?",
        "2. What time?",
        "↩️ Reply with the missing values (one per line), or tap ✏️ Edit to rewrite.",
    ]
    single = render_draft_card(_view(needs_info=["Which day?"], needs_info_flag=True))
    assert "1. Which day?\n↩️ Reply with the missing value, or tap ✏️ Edit to rewrite." in single
    # needs_info without a fillable placeholder: no numbered list, no fill hint.
    flagged = render_draft_card(_view(needs_info_flag=True))
    assert "❓ Needs info: see the draft" in flagged and "Reply with" not in flagged


def test_reply_gate_warning_lines() -> None:
    card = render_draft_card(_view(warnings=["drafted_by_habit"]))
    assert "✍️ Drafted: you usually reply to messages like this here" in card.split("\n")
    card = render_draft_card(_view(warnings=["drafted_on_request"]))
    assert "✍️ You asked for this draft" in card.split("\n")
    assert "⚠️ drafted_on_request" not in card


def test_incoming_is_truncated_and_card_fits_telegram() -> None:
    card = render_draft_card(_view(incoming=["x" * 2000], draft_text="y" * 5000))
    quoted = card.split("\n")[1]
    assert len(quoted) <= 2 + 600 and quoted.endswith("…")
    assert len(card) <= TELEGRAM_TEXT_MAX
    assert "(truncated)" in card


def test_keyboards_use_valid_callbacks() -> None:
    draft_id = uuid.uuid4()
    rows = draft_keyboard(draft_id, "en").inline_keyboard
    actions = [parse_callback(b.callback_data).action for row in rows for b in row]
    assert actions == ["approve", "edit", "regen", "reject", "sources"]
    assert all(parse_callback(b.callback_data).entity_id == str(draft_id) for r in rows for b in r)
    (confirm,) = confirm_send_keyboard("approve", draft_id, "en").inline_keyboard[0]
    assert confirm.callback_data == f"approve:{draft_id}:confirm"


def test_armed_card_shows_the_local_time_it_will_be_sent() -> None:
    sent_at = datetime(2026, 10, 14, 14, 32, tzinfo=UTC)
    card = render_draft_card(_view(auto_send_at=sent_at))
    lines = card.split("\n")
    assert "⏱ Will be sent automatically at 14:32" in lines
    assert lines.index("⏳ Expires 10:03 tomorrow") < lines.index(
        "⏱ Will be sent automatically at 14:32"
    )
    assert "⏱ Отправлю автоматически в 14:32" in render_draft_card(
        _view(auto_send_at=sent_at, lang="ru")
    )


def test_card_that_needs_approval_names_the_reasons() -> None:
    card = render_draft_card(_view(auto_send_reasons=("commitment", "sensitive")))
    assert "✋ Needs your approval: new promise, sensitive topic" in card
    assert "Will be sent automatically" not in card
    stopped = render_draft_card(_view(auto_send_stopped=True))
    assert "⏹ Automatic sending cancelled" in stopped
    assert "✋" not in stopped


def test_final_status_hides_the_auto_send_lines() -> None:
    card = render_draft_card(
        _view(
            auto_send_at=datetime(2026, 10, 14, 14, 32, tzinfo=UTC),
            auto_send_reasons=("commitment",),
            status="✅ Sent automatically at 14:32",
        )
    )
    assert "Will be sent automatically" not in card and "Needs your approval" not in card
    assert card.endswith("✅ Sent automatically at 14:32")


def test_armed_keyboard_has_send_now_and_stop() -> None:
    draft_id = uuid.uuid4()
    rows = armed_draft_keyboard(draft_id, "en").inline_keyboard
    assert [[b.callback_data for b in row] for row in rows] == [
        [f"approve:{draft_id}", f"edit:{draft_id}"],
        [f"autostop:{draft_id}"],
        [f"regen:{draft_id}", f"reject:{draft_id}", f"sources:{draft_id}"],
    ]
    assert rows[0][0].text == "✅ Send now" and rows[1][0].text == "⏹ Don't send automatically"
    assert armed_draft_keyboard(draft_id, "ru").inline_keyboard[1][0].text.startswith("⏹")


def test_possibly_unnecessary_banner() -> None:
    """004 FR-011: the 💤 banner names the card's contact; a finished card has none."""
    card = render_draft_card(_view(possibly_unnecessary=True))
    assert card.split("\n")[0] == "💤 Synthetic Anna: this may no longer be needed"
    ru = render_draft_card(_view(possibly_unnecessary=True, lang="ru"))
    assert ru.split("\n")[0] == "💤 Synthetic Anna: возможно, это уже не нужно"
    assert "💤" not in render_draft_card(_view(possibly_unnecessary=True, status="✅ Sent 10:07"))
    assert "💤" not in render_draft_card(_view())


# --- specs/004: earlier messages on the card and in the Sources view (T018) -------------------


SENT = datetime(2026, 7, 1, 22, 30, tzinfo=UTC)  # 2 Jul 01:30 in Moscow


def _earlier(chat_id: int = -1001234567890, ref: str = "earlier:1") -> StoredEarlier:
    return StoredEarlier(
        ref=ref,
        chat_id=chat_id,
        message_id=77,
        sent_at=SENT,
        chat_title="Synthetic Project",
        sender="Synthetic Client",
        excerpt="The pilot price is 120 000,\nagreed",
    )


def _draft(earlier: list[StoredEarlier], refs: list[str]) -> Any:
    text = "Yes, 120 000 as agreed."
    sources = [{"kind": "message", "id": ref, "used_for": "the agreed price"} for ref in refs]
    output = draft_output(text, sources=sources)
    return SimpleNamespace(output_json=pack_output(output, [], None, earlier), text=text)


def _user(lang: str = "en", tz: str = "Europe/Moscow") -> Any:
    return SimpleNamespace(id=uuid.uuid4(), language=lang, timezone=tz)


async def _labels(draft: Any, user: Any, *, detailed: bool = False) -> list[str]:
    return await source_labels(cast(Any, None), user, draft, detailed=detailed, tasks=[])


async def test_earlier_label_shows_chat_and_local_date() -> None:
    draft = _draft([_earlier()], ["earlier:1"])
    assert await _labels(draft, _user()) == ["💬 Synthetic Project · 2 Jul 2026"]
    assert await _labels(draft, _user(tz="UTC")) == ["💬 Synthetic Project · 1 Jul 2026"]
    assert await _labels(draft, _user("ru")) == ["💬 Synthetic Project · 2 июл 2026"]
    card = render_draft_card(_view(sources=await _labels(draft, _user())))
    assert "📎 Sources: 💬 Synthetic Project · 2 Jul 2026" in card


async def test_earlier_sources_view_has_sender_time_excerpt_and_link() -> None:
    draft = _draft([_earlier()], ["earlier:1"])
    [label] = await _labels(draft, _user(), detailed=True)
    assert label == (
        "💬 Synthetic Project · Synthetic Client · 2 Jul 2026 01:30 — the agreed price\n"
        "  «The pilot price is 120 000, agreed»\n"
        "  https://t.me/c/1234567890/77"
    )
    # Not a supergroup (a basic group or a private chat): no link.
    basic = _draft([_earlier(chat_id=-4123)], ["earlier:1"])
    [label] = await _labels(basic, _user(), detailed=True)
    assert "t.me" not in label and label.endswith("«The pilot price is 120 000, agreed»")


async def test_earlier_label_falls_back_when_the_entry_is_missing() -> None:
    draft = _draft([], ["earlier:2"])
    assert await _labels(draft, _user()) == ["💬 earlier message"]
    assert await _labels(draft, _user("ru")) == ["💬 сообщение из прошлой переписки"]
    detailed = await _labels(draft, _user(), detailed=True)
    assert detailed == ["💬 earlier message — the agreed price"]


@pytest.mark.parametrize(
    ("code", "lang", "text"),
    [
        ("earlier_search_unavailable", "en", "⚠️ drafted without searching earlier conversations"),
        ("earlier_search_unavailable", "ru", "⚠️ черновик без поиска по прошлой переписке"),
        ("earlier_search_no_match", "en", "🔎 nothing found in earlier conversations"),
        ("earlier_search_no_match", "ru", "🔎 в прошлой переписке ничего не нашлось"),
        (
            "earlier_search_off",
            "en",
            "🔎 earlier conversations not searched (turned off in /settings)",
        ),
        ("earlier_search_off", "ru", "🔎 поиск по прошлой переписке выключен (/settings)"),
    ],
)
def test_earlier_search_warnings(code: str, lang: str, text: str) -> None:
    card = render_draft_card(_view(warnings=[code], lang=lang))
    assert text in card.split("\n")
