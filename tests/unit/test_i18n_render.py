"""Russian rendering of the control-bot texts (issue #8): cards, keyboards, login, time zones."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from aiogram.types import ForceReply, InlineKeyboardMarkup

from scribe.i18n import t
from scribe.telegram.control_bot.edit_prefill import edit_prompt
from scribe.telegram.control_bot.render import (
    DraftCardView,
    account_label,
    draft_keyboard,
    help_text,
    method_choice_keyboard,
    qr_caption,
    qr_keyboard,
    render_draft_card,
    telegram_status_card,
)
from scribe.telegram.control_bot.review import _confirm_text, _fill_mismatch_text
from scribe.telegram.control_bot.settings import added_chats_text
from scribe.telegram.control_bot.timezones import (
    confirmation_text,
    resolve_time_zone,
    time_zone_keyboard,
)

NOW = datetime(2026, 10, 21, 11, 5, tzinfo=UTC)


def _texts(markup: InlineKeyboardMarkup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def _view(**values: object) -> DraftCardView:
    base: dict[str, object] = {
        "contact_title": "Синтетическая Анна",
        "chat_kind": "group",
        "read_at": "10:03",
        "incoming": ["Когда будет отчёт?"],
        "profile_label": t("card.profile.global", "ru"),
        "tone_shift": "more_formal",
        "draft_text": "В пятницу.",
        "expires": "завтра в 10:03",
        "lang": "ru",
    }
    base.update(values)
    return DraftCardView(**base)  # type: ignore[arg-type]


def test_russian_draft_card() -> None:
    card = render_draft_card(
        _view(
            commitment="прислать отчёт (до 2026-10-23)",
            conflicts=["Отчёт к пятнице"],
            sensitive=True,
            sensitive_categories=["personal_data"],
            needs_info=["Какой день?", "Во сколько?"],
            sources=["a", "b", "c", "d", "e"],
            warnings=["weak_style_profile", "new_code"],
        )
    )
    lines = card.split("\n")
    assert lines[0] == "💬 Синтетическая Анна   ·   группа   ·   прочитано в 10:03"
    assert "✍️ Черновик (общий, сдвиг тона: формальнее)" in lines
    assert (
        "⚑ Отметки: 🤝 вы обещаете: прислать отчёт (до 2026-10-23)  ·  "
        "⚠️ противоречит «Отчёт к пятнице»  ·  🔒 чувствительное (персональные данные)"
    ) in lines
    assert "❓ Нужны данные:" in lines and "2. Во сколько?" in lines
    assert lines[lines.index("2. Во сколько?") + 1].startswith("↩️ Ответьте недостающими значениями")
    assert "📎 Источники: a · b · c · ещё 2" in lines
    assert "⚠️ слабый профиль стиля: пока мало ваших сообщений" in lines
    assert "⚠️ new_code" in lines  # an unknown code is shown as it is
    assert lines[-1] == "⏳ Истекает завтра в 10:03"


def test_russian_card_status_and_truncation() -> None:
    card = render_draft_card(_view(status="✅ Отправлено в 10:07", draft_text="я" * 5000))
    assert card.endswith("\n\n✅ Отправлено в 10:07")
    assert "… (обрезано)" in card and len(card) <= 4096


def test_russian_keyboards() -> None:
    assert _texts(draft_keyboard(uuid.uuid4(), "ru")) == [
        "✅ Отправить",
        "✏️ Изменить",
        "🔁 Заново",
        "❌ Отклонить",
        "🧾 Источники",
    ]
    assert _texts(method_choice_keyboard("ru"))[0] == "📷 Сканировать QR-код (рекомендуется)"
    assert _texts(qr_keyboard("ru", "tg://login?token=abc")) == [
        "📲 Подтвердить на этом устройстве",
        "📱 Войти по номеру телефона",
        "✖️ Отмена",
    ]


def test_russian_login_texts() -> None:
    caption = qr_caption(2, 5, datetime(2026, 10, 20, 11, 3, 27, tzinfo=UTC), "Europe/Moscow", "ru")
    assert caption.startswith("📷 Отсканируйте код, чтобы подключиться\n")
    assert "«Настройки → Устройства → Подключить устройство»" in caption
    assert caption.endswith("\nКод 2 из 5 · действует до 14:03:27")
    assert account_label("", None, None, "ru") == "Неизвестный аккаунт"
    assert t("connect.rate_limited", "ru", seconds=42) == (
        "Telegram просит подождать 42 с перед следующей попыткой."
    )


def test_russian_telegram_status_card() -> None:
    card = telegram_status_card(
        account="Синтетик (@synthetic)",
        status="revoked",
        logged_out_elsewhere=True,
        login_method="qr",
        connected_at=datetime(2026, 5, 20, 11, 3, tzinfo=UTC),
        tz="Europe/Moscow",
        device_model="Scribe",
        lang="ru",
    )
    assert card.split("\n") == [
        "📡 Подключение Telegram",
        "Аккаунт: Синтетик (@synthetic)",
        "Статус: ⚠️ отключён (выход выполнен с другого устройства)",
        "Подключён: 20 мая 2026, 14:03 · по QR-коду",
        "",
        "Чтобы завершить подключение самостоятельно: Telegram → Настройки → Устройства → "
        "«Scribe» → Завершить сеанс.",
    ]


def test_russian_time_zone_texts() -> None:
    text = confirmation_text(resolve_time_zone("+3", NOW), NOW, "ru")
    assert text.startswith(
        "✅ Часовой пояс: Etc/GMT-3 (UTC+3, там сейчас 14:05).\n💡 Фиксированное"
    )
    labels = _texts(
        time_zone_keyboard("settz", ("Europe/Moscow", "Asia/Novosibirsk", "UTC"), NOW, "ru")
    )
    assert labels == ["Москва (UTC+3)", "Новосибирск (UTC+7)", "UTC"]
    # Zones without a catalog name keep the IANA city.
    assert _texts(time_zone_keyboard("settz", ("Pacific/Fiji",), NOW, "ru")) == ["Fiji (UTC+12)"]


def test_russian_edit_prompts() -> None:
    text, markup, _ = edit_prompt("Коротко.", inline=True, lang="ru")
    assert text == t("edit.inline_prompt", "ru")
    assert isinstance(markup, InlineKeyboardMarkup)
    assert _texts(markup) == ["✏️ Изменить в поле ввода"]
    text, reply, _ = edit_prompt("", inline=False, lang="ru")
    assert text == t("edit.prompt", "ru")
    assert isinstance(reply, ForceReply) and reply.input_field_placeholder == "Ваш вариант…"


def test_russian_review_texts_with_plurals() -> None:
    assert _confirm_text(["review.reason.placeholders", "review.reason.sensitive"], "ru") == (
        "⚠️ В черновике незаполненные поля и чувствительные данные. Всё равно отправить?"
    )
    assert _confirm_text(["review.reason.sensitive"], "en") == (
        "⚠️ This draft has sensitive content. Send anyway?"
    )
    one = _fill_mismatch_text(["Какой день?"], 2, "ru")
    assert one.startswith("⚠️ Для этого черновика нужно одно значение:\n1. Какой день?\n")
    assert "Вы прислали: 2." in one
    three = _fill_mismatch_text(["a", "b", "c"], 1, "ru")
    assert three.startswith("⚠️ Для этого черновика нужно 3 значения, по одному в строке")
    five = _fill_mismatch_text(list("abcde"), 1, "ru")
    assert five.startswith("⚠️ Для этого черновика нужно 5 значений, по одному в строке")


def test_help_text_in_both_languages() -> None:
    assert help_text("en").startswith("Scribe drafts replies to your work chats")
    assert "/pause — Приостановить черновики" in help_text("ru")


def test_added_chats_history_label() -> None:
    """specs/007: at most 3 titles, then "+N more"; no titles left -> no suffix."""
    assert added_chats_text(["Project X", "Anna"], "en") == "Project X, Anna"
    five = ["Design", "Ops", "Finance", "Legal", "HR"]
    assert added_chats_text(five, "en") == "Design, Ops, Finance, +2 more"
    assert added_chats_text(five, "ru") == "Design, Ops, Finance, ещё 2"
    assert added_chats_text([], "en") is None
    assert t("history.kind.chats_added", "en") == "➕ Work chats added"
    assert t("history.kind.chats_added", "ru") == "➕ Добавлены рабочие чаты"
    assert t("chats.added", "ru", n=1).startswith("✅ Добавлен 1 чат.")
    assert t("chats.added", "ru", n=3).startswith("✅ Добавлено 3 чата.")
    assert t("chats.added", "ru", n=5).startswith("✅ Добавлено 5 чатов.")
    assert t("chats.added", "en", n=1).startswith("✅ Added 1 chat.")
