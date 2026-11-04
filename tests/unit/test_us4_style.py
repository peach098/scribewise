"""US4 pure helpers: edit features behind rule proposals (FR-033), user-rule editing, the
"what changed" note of the weekly refresh (FR-037), language override parsing (FR-024) and the
callback actions the ownership middleware checks (T072). Synthetic data only."""

from __future__ import annotations

import functools
import re
import types
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from aiogram import Router
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.db.models import Contact, ContactKind, ProfileScope, StyleProfile, User
from scribe.i18n import Msg
from scribe.style.profiles import (
    InvalidRule,
    RefreshResult,
    add_rule,
    describe_changes,
    profile_context,
    refresh_note,
    resolve_user_rules,
    same_rule,
    user_rules,
    with_rule_added,
    with_rule_removed,
)
from scribe.style.proposals import FEATURES, edit_features, proposal_text, rule_label
from scribe.telegram.control_bot.bot import create_dispatcher
from scribe.telegram.control_bot.ownership import ENTITY_ACTIONS, check_callback
from scribe.telegram.control_bot.profile_command import profile_text, rules_lines
from scribe.telegram.control_bot.render import TELEGRAM_TEXT_MAX, utf16_len
from scribe.telegram.control_bot.settings import InvalidSetting, parse_language


@pytest.mark.parametrize(
    ("draft", "edited", "expected"),
    [
        ("Привет! Отчёт в пятницу 🙂", "Привет! Отчёт в пятницу", {"emoji_removed"}),
        ("Report on Friday", "Report on Friday 👍", {"emoji_added"}),
        ("Привет! Отчёт будет в пятницу", "Отчёт будет в пятницу", {"greeting_removed"}),
        (
            "The report is ready, I'll send it today.\nBest, Anna",
            "The report is ready, I'll send it today.",
            {"sign_off_removed"},
        ),
        (
            "Thanks for the reminder, the report will be ready on Friday afternoon",
            "Friday",
            {"shorter"},
        ),
        ("ок, давай завтра", "Хорошо, пожалуйста, давайте завтра", {"more_formal"}),
        (
            "Отчёт будет готов в пятницу после обеда",
            "The report will be ready on Friday afternoon",
            {"language_en"},
        ),
        ("Same text here", "Same text here", set()),
    ],
)
def test_edit_features(draft: str, edited: str, expected: set[str]) -> None:
    assert edit_features(draft, edited) == expected


def test_every_feature_has_a_rule_and_a_message() -> None:
    for key, spec in FEATURES.items():
        assert spec.rule and spec.noticed, key
    assert proposal_text("sign_off_removed", 5, 6, "Anna") == (
        "💡 I noticed you removed the sign-off in 5 of your last 6 edits for Anna.\n"
        'Add rule "no sign-off" to Anna\'s profile?'
    )
    assert proposal_text("sign_off_removed", 5, 6, "Anna", "ru") == (
        "💡 Я заметил, что вы убирали подпись в конце в 5 из 6 последних правок для контакта "
        "Anna.\nДобавить в профиль контакта Anna правило «без подписи в конце»?"
    )
    for key in FEATURES:
        assert rule_label(FEATURES[key].rule, key, "ru") != FEATURES[key].rule, key
    assert rule_label("never use emojis", None, "ru") == "never use emojis"


def test_with_rule_added_uses_the_same_rule_identity_as_the_merge() -> None:
    """One definition of "the same rule" (issue #42): text with case and spacing ignored, or the
    same proposal key."""
    stored = {"rules": [
        {"text": "no  SIGN-OFF", "source": "user"},
        {"text": "без эмодзи", "source": "proposal", "key": "emoji_removed"},
    ]}  # fmt: skip
    assert with_rule_added(stored, "No sign-off", lang="en") == stored
    assert (
        with_rule_added(stored, "no emojis", source="proposal", key="emoji_removed", lang="en")
        == stored
    )
    assert (
        len(user_rules(with_rule_added(stored, "no emojis", lang="en"))) == 3
    )  # no key: a new rule


def test_user_rules_add_dedupe_and_remove() -> None:
    rules = with_rule_added({}, "  never   use emojis ", lang="en")
    rules = with_rule_added(rules, "Never use emojis", lang="en")  # same rule: kept once
    rules = with_rule_added(
        rules, "no sign-off", source="proposal", key="sign_off_removed", lang="en"
    )
    assert user_rules(rules) == [
        {"text": "never use emojis", "source": "user"},
        {"text": "no sign-off", "source": "proposal", "key": "sign_off_removed"},
    ]
    assert [r["text"] for r in user_rules(with_rule_removed(rules, 1))] == ["no sign-off"]
    with pytest.raises(InvalidRule):
        with_rule_removed(rules, 3)
    with pytest.raises(InvalidRule):
        with_rule_added(rules, "   ", lang="en")
    with pytest.raises(InvalidRule):
        with_rule_added(rules, "x" * 201, lang="en")


def _rules(*texts: str) -> dict[str, Any]:
    return {"rules": [{"text": text, "source": "user"} for text in texts]}


def _style_profile(scope: ProfileScope, *texts: str) -> StyleProfile:
    return StyleProfile(
        scope=scope,
        rules_json={"formality": 3},
        user_rules_json=_rules(*texts),
        examples=["Synthetic example"],
        version=1,
        sample_size=120,
        summary_text="Synthetic summary.",
    )


def test_resolved_rules_order_dedupe_and_precedence() -> None:
    """Issue #42: global rules first, then the contact's own; one entry per rule (case and
    spacing ignored, or the same proposal ``key``), the contact's copy kept, so the contact rule
    wins on conflict."""
    contact = _style_profile(ProfileScope.contact)
    contact.user_rules_json = {"rules": [
        {"text": "no  SIGN-OFF", "source": "user"},
        {"text": "write casually with Ivan", "source": "user"},
        {"text": "без эмодзи", "source": "proposal", "key": "emoji_removed"},
    ]}  # fmt: skip
    global_rules = {"rules": [
        {"text": "never use emojis", "source": "user"},
        {"text": "No sign-off", "source": "proposal", "key": "sign_off_removed"},
        {"text": "no emojis", "source": "proposal", "key": "emoji_removed"},
        {"text": "write formally", "source": "user"},
    ]}  # fmt: skip
    resolved = resolve_user_rules(contact, global_rules, lang="en")
    assert resolved.user_rules_json() == {
        "rules": [
            {"text": "never use emojis", "source": "user", "scope": "global"},
            {"text": "write formally", "source": "user", "scope": "global"},
            {"text": "no  SIGN-OFF", "source": "user", "scope": "contact"},
            {"text": "write casually with Ivan", "source": "user", "scope": "contact"},
            {"text": "без эмодзи", "source": "proposal", "key": "emoji_removed",
             "scope": "contact"},
        ]
    }  # fmt: skip
    assert [r["text"] for r in resolved.inherited] == ["never use emojis", "write formally"]
    assert resolved.inherits("NEVER use  emojis")
    assert resolved.inherits("something else", "sign_off_removed") is False  # the contact's own
    # The profile row is left as it is (it keeps only its own rules).
    assert len(contact.user_rules_json["rules"]) == 3
    # A global profile inherits nothing; its rules go as stored, without a scope.
    global_ = _style_profile(ProfileScope.global_, "a", "b")
    assert resolve_user_rules(global_, _rules("ignored"), lang="en").user_rules_json() == _rules(
        "a", "b"
    )
    assert resolve_user_rules(None, _rules("ignored"), lang="en").user_rules_json() == {"rules": []}


def test_add_rule_skips_a_rule_that_applies_through_the_global_profile() -> None:
    """The one "already global" guard (typed rules and accepted proposals); an accepted
    proposal's localized label (what the user sees) is the same rule in the user's language."""
    contact = _style_profile(ProfileScope.contact, "reply in English")
    global_rules = {"rules": [{"text": "no emojis", "source": "proposal", "key": "emoji_removed"}]}
    applied = resolve_user_rules(contact, global_rules, lang="ru")
    assert applied.inherits("без  ЭМОДЗИ")
    assert not resolve_user_rules(contact, global_rules, lang="en").inherits("без эмодзи")
    typed = add_rule(applied, contact.user_rules_json, "Без  эмодзи")
    assert typed.already_global and typed.user_rules_json == contact.user_rules_json
    proposed = add_rule(
        applied, contact.user_rules_json, "no emojis", source="proposal", key="emoji_removed"
    )
    assert proposed.already_global and proposed.user_rules_json == contact.user_rules_json
    added = add_rule(applied, contact.user_rules_json, "be brief")
    assert not added.already_global
    assert [r["text"] for r in user_rules(added.user_rules_json)] == [
        "reply in English",
        "be brief",
    ]


def test_rule_identity_by_the_label_shown_in_the_users_language() -> None:
    """Issue #42 review: with the user's language, a keyed rule is also its displayed label,
    but only when ``rule_label`` would show that label (the standard text for the key)."""
    keyed = {"text": "no emojis", "source": "proposal", "key": "emoji_removed"}
    # (c) the global profile's own keyed rule: typing its ru label is a duplicate.
    own = {"rules": [keyed]}
    assert with_rule_added(own, "Без эмодзи", lang="ru") == own
    assert len(user_rules(with_rule_added(own, "Без эмодзи", lang="en"))) == 2
    # (b) a contact's typed "без эмодзи" and the global keyed rule: listed once, the contact's.
    contact = _style_profile(ProfileScope.contact, "без эмодзи")
    resolved = resolve_user_rules(contact, own, lang="ru")
    assert resolved.inherited == []
    assert resolved.user_rules_json() == {
        "rules": [{"text": "без эмодзи", "source": "user", "scope": "contact"}]
    }
    # A keyed rule whose text is not the catalog's standard text is shown as stored, so its
    # label does not match.
    odd = {"text": "never any emoji", "source": "proposal", "key": "emoji_removed"}
    assert not same_rule(odd, {"text": "без эмодзи", "source": "user"}, lang="ru")
    assert same_rule(keyed, {"text": "без эмодзи", "source": "user"}, lang="ru")
    assert same_rule(odd, keyed, lang="ru")  # same key


def test_profile_context_carries_the_resolved_rules() -> None:
    """The profile block holds the learned style only; the resolved user rules go in their own
    block (specs/013)."""
    contact = _style_profile(ProfileScope.contact, "reply in English")
    context = profile_context(contact)
    assert "user_rules_json" not in context.model_dump()
    rules = resolve_user_rules(contact, _rules("never use emojis"), lang="en").user_rules_json()
    assert [(r["text"], r["scope"]) for r in rules["rules"]] == [
        ("never use emojis", "global"),
        ("reply in English", "contact"),
    ]
    assert context.rules_json == {"formality": 3} and context.examples == ["Synthetic example"]
    global_ = _style_profile(ProfileScope.global_, "never use emojis")
    assert resolve_user_rules(global_, {}, lang="en").user_rules_json() == _rules(
        "never use emojis"
    )


def test_rules_lines_show_inherited_global_rules_read_only() -> None:
    contact = _style_profile(ProfileScope.contact, "reply in English", "no emojis")
    lines = rules_lines(
        resolve_user_rules(contact, _rules("never use emojis", "No emojis", "be brief"), lang="en"),
        "en",
    )
    assert lines == [
        "Rules you set:",
        "1. reply in English",
        "2. no emojis",
        "Global rules (apply here too; edit them in your global profile):",
        "• never use emojis",
        "• be brief",
    ]
    empty = _style_profile(ProfileScope.contact)
    assert rules_lines(resolve_user_rules(empty, _rules("be brief"), lang="en"), "ru") == [
        "Ваши правила: пока нет",
        "Общие правила (действуют и здесь; меняются в общем профиле):",
        "• be brief",
    ]
    assert rules_lines(resolve_user_rules(contact, {}, lang="en"), "en") == [
        "Rules you set:",
        "1. reply in English",
        "2. no emojis",
    ]


def test_contact_profile_view_fits_a_telegram_message(settings: Settings) -> None:
    """30 own + 30 global rules of 200 characters exceed 4096: the inherited global rules are
    cut first ("…and N more"), then the contact's own (never past what fits)."""
    contact = _style_profile(
        ProfileScope.contact, *(f"own {n:02d} " + "x" * 193 for n in range(30))
    )
    global_rules = _rules(*(f"global {n:02d} " + "y" * 190 for n in range(30)))
    resolved = resolve_user_rules(contact, global_rules, lang="en")
    owner = _contact("Anna")
    text = profile_text(contact, resolved, owner=owner, settings=settings, lang="en")
    assert len(text) <= TELEGRAM_TEXT_MAX
    assert "1. own 00" in text and "• global" not in text
    assert "…and 30 more global rules: see your global profile." in text
    assert re.search(r"…and \d+ more rules of yours\.", text)
    # Only the global list too long: own rules all shown, global ones cut.
    few = _style_profile(ProfileScope.contact, "reply in English")
    lines = rules_lines(resolve_user_rules(few, global_rules, lang="en"), "ru", budget=1200)
    joined = "\n".join(lines)
    assert len(joined) <= 1200 and "1. reply in English" in joined and "• global 00" in joined
    assert re.search(r"…и ещё \d+ общих? правил\w*: смотрите общий профиль\.", joined)
    # Telegram counts UTF-16 code units: an emoji is two of them.
    emoji = _style_profile(ProfileScope.contact, *(f"{n} " + "🙂" * 150 for n in range(12)))
    resolved = resolve_user_rules(
        emoji, _rules(*(f"{n} " + "👍" * 150 for n in range(12))), lang="en"
    )
    full = "\n".join(rules_lines(resolved, "en", budget=10**6))
    assert len(full) <= TELEGRAM_TEXT_MAX < utf16_len(full)
    text = profile_text(emoji, resolved, owner=owner, settings=settings, lang="en")
    assert utf16_len(text) <= TELEGRAM_TEXT_MAX and "more global rules" in text
    one = rules_lines(
        resolve_user_rules(_style_profile(ProfileScope.contact, "a" * 50, "b" * 50), {}, lang="en"),
        "en",
        budget=100,
    )
    assert one[-1] == "…and 1 more rule of yours."
    # Short lists are untouched.
    assert (
        rules_lines(resolve_user_rules(few, _rules("x"), lang="en"), "en", budget=1200)[-1] == "• x"
    )


def _contact(name: str, kind: ContactKind = ContactKind.person) -> Contact:
    return Contact(display_name=name, kind=kind, telegram_peer_id=1, own_message_count_6m=0)


def test_describe_changes_and_note() -> None:
    old = {"formality": 3, "typical_length_words": 40, "emoji_usage": "rare", "languages": ["ru"]}
    new = {"formality": 4, "typical_length_words": 20, "emoji_usage": "none", "languages": ["en"]}
    assert [m.render("en") for m in describe_changes(_contact("Ivan"), old, new)] == [
        "more formal with Ivan",
        "shorter replies with Ivan",
        "fewer emojis with Ivan",
        "mostly en with Ivan",
    ]
    finance = _contact("Finance", ContactKind.group)
    assert [m.render("en") for m in describe_changes(finance, None, new)] == [
        'new profile with "Finance" group'
    ]
    assert [m.render("ru") for m in describe_changes(finance, None, new)] == [
        "новый профиль в группе «Finance»"
    ]
    assert describe_changes(None, old, old) == []
    ivan = Msg("refresh.where.contact", name="Ivan")
    note = refresh_note(
        RefreshResult(
            batch_id=None, global_refreshed=True, contacts_refreshed=3, retired=1,
            changes=[Msg("refresh.change.shorter", where=ivan)],
        )
    )  # fmt: skip
    assert note.startswith(
        "🔄 Profiles refreshed (global + 3 contacts). Changes: shorter replies with Ivan."
    )
    assert "Retired 1 contact profile" in note and note.endswith("Your own rules are unchanged.")
    assert "no notable changes" in refresh_note(RefreshResult(batch_id=None, global_refreshed=True))
    ru = refresh_note(
        RefreshResult(batch_id=None, global_refreshed=True, contacts_refreshed=3, retired=1), "ru"
    )
    assert ru.startswith("🔄 Профили обновлены (общий + 3 контакта). Изменения: заметных")
    assert "Снят 1 профиль контакта" in ru
    assert ru.endswith("Ваши собственные правила не изменились.")


@pytest.mark.parametrize(
    ("text", "expected"),
    [("en", "en"), ("EN", "en"), ("pt_br", "pt-BR"), ("zh-hant", "zh-Hant"),
     ("es-419", "es-419"), ("English", "en"), ("русский", "ru")],
)  # fmt: skip
def test_parse_language(text: str, expected: str) -> None:
    assert parse_language(text) == expected


@pytest.mark.parametrize("text", ["", "e", "english please", "en-", "12", "en-US-x-y"])
def test_parse_language_rejects(text: str) -> None:
    with pytest.raises(InvalidSetting):
        parse_language(text)


# Callbacks that name no stored row: settings menus, Trello board ids and pages, the export,
# the contact-profile list, Rebuild, the setup chat picker (Telegram dialog ids) and the
# Telegram connect buttons of specs/002-telegram-qr-login (owner-checked there), and the time
# zone / language buttons (an IANA zone or a language code, issue #8).
NO_ENTITY_ACTIONS = {
    "settings",
    "setday",
    "settz",
    "setuptz",
    "setlang",
    "tasks",
    "tboard",
    "tpage",
    "jira",  # specs/003: the Jira connect flow acts on the caller's own FSM data only
    "jdep",
    "jcert",
    "mydata",
    "apply",  # specs/005 US5: the applicant's own FSM data; no users row to own anything
    "betaapp",  # specs/005 US5: applications are not user-owned; an operator filter guards it
    "profcontacts",
    "profrebuild",
    "pickchat",
    "pickpage",
    "pickdone",
    "chatadd",  # specs/007: ➕ Add work chats acts on the caller's own rows and FSM data only
    "pickwa",  # specs/008: the setup picker's work account switch (the caller's FSM data)
    "wacct",  # specs/008: wacct:<on|off|cancel>, the caller's own users row
    "chatspage",  # specs/008: /chats paging
    "wxopen",  # specs/008: the exclusion picker (the caller's own rows and FSM data)
    "tglogin",
    "tgreconnect",
    "tgdisconnect",
    "skipcfg",  # 004: skipcfg:global:<on|off>, the caller's own users row
    "skippage",
    "skipchats",
    "setgender",  # issue #46: setgender:<masculine|feminine>, the caller's own users row
}
# specs/014: ownership is listed for these before US2 / US3 register their handlers (T028, T033);
# empty this set when they do.
AUTO_SEND_HANDLERS_PENDING = {"autooffer", "autowarn"}
_ACTION_IN_FILTER = re.compile(r"^\^?([a-z_]+):")


def _strings(obj: object, depth: int = 0) -> Iterator[str]:
    """Every string (and regex pattern) inside a magic filter's operations."""
    if depth > 8:
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, re.Pattern):
        yield obj.pattern
    elif isinstance(obj, tuple | list | set | frozenset):
        for item in obj:
            yield from _strings(item, depth + 1)
    elif isinstance(obj, dict):
        for item in obj.values():
            yield from _strings(item, depth + 1)
    elif isinstance(obj, functools.partial):
        yield from _strings((obj.func, obj.args, obj.keywords), depth + 1)
    else:
        bound = getattr(obj, "__self__", None)
        if bound is not None and not isinstance(bound, types.ModuleType):
            yield from _strings(bound, depth + 1)
        for slot in getattr(type(obj), "__slots__", ()) or ():
            if hasattr(obj, slot):
                yield from _strings(getattr(obj, slot), depth + 1)
        if hasattr(obj, "__dict__") and not callable(obj):
            for item in vars(obj).values():
                yield from _strings(item, depth + 1)


def _routers(router: Router) -> Iterator[Router]:
    yield router
    for sub in router.sub_routers:
        yield from _routers(sub)


def registered_callback_actions() -> dict[str, set[str]]:
    """Handler name -> the callback actions its filters match, from the real dispatcher."""
    dispatcher = create_dispatcher(
        session_factory=async_sessionmaker(), settings=None,  # type: ignore[arg-type]
        storage=MemoryStorage(),
    )  # fmt: skip
    found: dict[str, set[str]] = {}
    for router in _routers(dispatcher):
        for handler in router.callback_query.handlers:
            name = f"{router.name}.{getattr(handler.callback, '__name__', handler.callback)}"
            actions = found.setdefault(name, set())
            for flt in handler.filters or []:
                if flt.magic is None:
                    continue
                for value in _strings(flt.magic._operations):
                    match = _ACTION_IN_FILTER.match(value)
                    if match:
                        actions.add(match.group(1))
    return found


def test_ownership_covers_every_entity_action() -> None:
    by_handler = registered_callback_actions()
    unparsed = [name for name, actions in by_handler.items() if not actions]
    assert unparsed == []  # every callback handler's action could be read from its filter
    registered = set().union(*by_handler.values())
    assert {"approve", "pin", "rule_accept", "cmtres", "chatenable"} <= registered
    missing = registered - NO_ENTITY_ACTIONS - set(ENTITY_ACTIONS)
    assert missing == set(), f"callback actions without an ownership check: {sorted(missing)}"
    # No stale entries either; the offer and warning buttons are listed ahead of their handlers.
    assert set(ENTITY_ACTIONS) - AUTO_SEND_HANDLERS_PENDING <= registered


AUTO_SEND_ENTITY_ACTIONS = {
    "autostop": "Draft",
    "autooffer": "WorkChat",
    "autoconfirm": "WorkChat",
    "autowarn": "WorkChat",
    "chatauto": "WorkChat",
}


def test_ownership_lists_every_auto_send_entity_action() -> None:
    """specs/014: a card or chat button that is not listed would skip the ownership check."""
    assert {a: ENTITY_ACTIONS[a][0].__name__ for a in AUTO_SEND_ENTITY_ACTIONS} == (
        AUTO_SEND_ENTITY_ACTIONS
    )
    assert "autowin" not in ENTITY_ACTIONS  # autowin:<seconds> names no row


@pytest.mark.parametrize(
    "data",
    [
        "approve:",
        "pin:a:b:c",
        "chatenable::off",
        "undo_refresh:not-a-uuid",
        "cmtdel:1:2:3:4",
        "autostop:",
        "autostop:not-a-uuid",
        "autoconfirm:::manual",
        "chatauto:not-a-uuid:on",
    ],
)
async def test_ownership_refuses_malformed_entity_callbacks(data: str) -> None:
    user = User(id=uuid.uuid4(), telegram_control_chat_id=1)

    def no_database() -> AsyncSession:
        raise AssertionError("malformed data must be refused before any query")

    refusal = await check_callback(data, user, no_database)  # type: ignore[arg-type]
    assert refusal == ENTITY_ACTIONS[data.split(":", 1)[0]][1]


async def test_ownership_passes_non_entity_callbacks() -> None:
    user = User(id=uuid.uuid4(), telegram_control_chat_id=1)
    for data in ("settings:delay", "setday:3", "profrebuild:1", "nonsense", "mydata:export"):
        assert await check_callback(data, user, None) is None  # type: ignore[arg-type]
