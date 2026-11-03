"""Best-effort lint (issue #8): no hard-coded user-facing text in the modules that talk to the
user, and every catalog key used in code exists.

- Texts passed to ``answer`` / ``send_message`` / ``edit_message_text`` / ``send_photo``
  (``text=`` / ``caption=``), to button and command constructors, and to ``ForceReply`` must
  come from the catalogs: a string literal there may only hold emoji, punctuation or
  placeholders, never words.
- No module-level text constants (``FOO_TEXT = "Some words"``) in those modules.
- ``t("key", ...)``, ``Msg("key", ...)`` and ``LocalizedError`` subclasses' literal keys exist in
  the catalogs.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from scribe.i18n import CATALOGS

SRC = Path(__file__).resolve().parents[2] / "src" / "scribe"
USER_FACING_MODULES = sorted(
    [
        *(SRC / "telegram" / "control_bot").glob("*.py"),
        SRC / "access" / "gate.py",
        SRC / "access" / "apply.py",
        SRC / "telegram" / "client_pool.py",
        SRC / "intake" / "router.py",
        SRC / "intake" / "events.py",
        SRC / "scheduling" / "deadlines.py",
        SRC / "jobs.py",
        SRC / "__main__.py",
    ]
)
SEND_METHODS = frozenset(
    {
        "answer",
        "reply",
        "send_message",
        "edit_message_text",
        "send_photo",
        "edit_message_caption",
        "answer_photo",
    }
)
TEXT_CONSTRUCTORS = frozenset({"button", "InlineKeyboardButton", "BotCommand", "ForceReply"})
TEXT_KEYWORDS = frozenset({"text", "caption", "description", "input_field_placeholder"})
KEY_FUNCTIONS = frozenset({"t", "Msg"})
KEY_ERRORS = frozenset(
    {"InvalidSetting", "UnknownTimeZone", "LocalizedError", "InvalidCommitmentEdit", "InvalidRule"}
)
_WORD = re.compile(r"[A-Za-zА-Яа-яЁё]{2,}")
_KEY = re.compile(r"^[a-z_]+(\.[A-Za-z0-9_/]+)+$")


def _literal_words(node: ast.expr) -> str | None:
    """The words in a string literal (or the literal parts of an f-string), if any."""
    parts: list[str] = []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        parts.append(node.value)
    elif isinstance(node, ast.JoinedStr):
        parts += [
            v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)
        ]
    elif isinstance(node, ast.BinOp):
        return _literal_words(node.left) or _literal_words(node.right)
    text = "".join(parts)
    if _KEY.match(text):
        return None
    return text if _WORD.search(text) else None


def _name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _text_args(call: ast.Call) -> list[ast.expr]:
    name = _name(call.func)
    if name in SEND_METHODS:
        # bot.send_message(chat_id, text), message.answer(text), callback.answer(text)
        positional = call.args[1:2] if name in {"send_message", "send_photo"} else call.args[:1]
        return [*positional, *(k.value for k in call.keywords if k.arg in TEXT_KEYWORDS)]
    if name in TEXT_CONSTRUCTORS:
        return [*call.args[:1], *(k.value for k in call.keywords if k.arg in TEXT_KEYWORDS)]
    return []


def _module_id(path: Path) -> str:
    return str(path.relative_to(SRC))


@pytest.mark.parametrize("path", USER_FACING_MODULES, ids=_module_id)
def test_no_hard_coded_texts_sent_to_the_user(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = [
        f"{_module_id(path)}:{node.lineno}: {words!r}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for arg in _text_args(node)
        if (words := _literal_words(arg)) is not None
    ]
    assert found == [], "user-facing literals; use scribe.i18n.t(...):\n" + "\n".join(found)


@pytest.mark.parametrize("path", USER_FACING_MODULES, ids=_module_id)
def test_no_module_level_text_constants(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in tree.body:
        if not isinstance(node, ast.Assign | ast.AnnAssign) or node.value is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        value = node.value
        if (
            any(n.isupper() for n in names)
            and isinstance(value, ast.Constant | ast.JoinedStr)
            and (words := _literal_words(value)) is not None
            and " " in words
        ):
            found.append(f"{_module_id(path)}:{node.lineno}: {names[0]}")
    assert found == [], "text constants; move them to scribe/i18n/en.py + ru.py:\n" + "\n".join(
        found
    )


def _used_keys() -> list[tuple[str, str]]:
    used = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            if _name(node.func) not in KEY_FUNCTIONS | KEY_ERRORS:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                used.append((f"{_module_id(path)}:{node.lineno}", first.value))
    return used


def test_used_catalog_keys_exist() -> None:
    used = _used_keys()
    assert len(used) > 100  # the scan really sees the code
    missing = [f"{where}: {key!r}" for where, key in used if key not in CATALOGS["en"]]
    assert missing == [], "unknown catalog keys:\n" + "\n".join(missing)


def test_group_mode_texts_are_gone() -> None:
    """006 FR-004/FR-007: the group-mode choice has no texts left; the fixed rule and the
    stale-button note exist in every language."""
    removed = {
        "settings.group_mode.always",
        "settings.group_mode.mentions_only",
        "btn.group_mentions_only",
        "btn.group_always",
    }
    for lang in ("en", "ru"):
        catalog = CATALOGS[lang]
        assert not removed & set(catalog), lang
        for key in ("alert.option_removed", "settings.chat_group"):
            assert key in catalog, (lang, key)
            assert "{mode}" not in catalog[key], (lang, key)


def test_lint_catches_a_hard_coded_text() -> None:
    sample = ast.parse('async def f(m):\n    await m.answer("Hello there")\n')
    call = next(n for n in ast.walk(sample) if isinstance(n, ast.Call))
    assert [_literal_words(a) for a in _text_args(call)] == ["Hello there"]
    ok = ast.parse('async def f(m):\n    await m.answer(t("common.not_set_up", lang))\n')
    call = next(n for n in ast.walk(ok) if isinstance(n, ast.Call) and _name(n.func) == "answer")
    assert [_literal_words(a) for a in _text_args(call)] == [None]
