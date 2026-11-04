"""Principle I, statically: ``sending/send.py`` is the only caller of ``send_text`` and is reached
only from the review module's approve/edit path and from the auto-send step handler (specs/014),
which passes ``auto=True`` and nothing else."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "scribe"
# Gateway implementations delegate send_text to the per-user client; they are not callers.
GATEWAY_IMPLEMENTATIONS = {"telegram/client_pool.py"}


def _calls(name: str) -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if called == name:
                found.setdefault(path.relative_to(SRC).as_posix(), []).append(node.lineno)
    return found


def test_send_text_only_called_from_send_module() -> None:
    callers = set(_calls("send_text")) - GATEWAY_IMPLEMENTATIONS
    assert callers == {"sending/send.py"}


def _owners(tree: ast.AST, name: str) -> set[str]:
    """Names of the functions in ``tree`` that call ``name``."""
    return {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.AsyncFunctionDef | ast.FunctionDef)
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == name
    }


def test_send_approved_draft_only_called_from_review_approval_path_and_auto_sender() -> None:
    assert set(_calls("send_approved_draft")) == {
        "telegram/control_bot/review.py",
        "sending/auto_send.py",
    }
    review = ast.parse((SRC / "telegram/control_bot/review.py").read_text(encoding="utf-8"))
    assert _owners(review, "send_approved_draft") == {"_approve_and_send"}
    assert _owners(review, "_approve_and_send") == {"on_approve", "on_edit", "on_edited_text"}
    auto = ast.parse((SRC / "sending/auto_send.py").read_text(encoding="utf-8"))
    assert _owners(auto, "send_approved_draft") == {"_send"}
    (call,) = (
        node
        for node in ast.walk(auto)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "send_approved_draft"
    )
    keywords = {k.arg: k.value for k in call.keywords}
    assert isinstance(keywords["auto"], ast.Constant) and keywords["auto"].value is True
    assert "edited_text" not in keywords and "allow_outdated" not in keywords


def test_only_the_auto_sender_sets_auto_approved_at() -> None:
    """The user's approval column is written by the review callbacks, the automatic one only by
    the auto-send step handler."""
    writers: dict[str, set[str]] = {"auto_approved_at": set(), "approved_by_user_at": set()}
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Attribute) and target.attr in writers:
                        writers[target.attr].add(path.relative_to(SRC).as_posix())
    assert writers["auto_approved_at"] == {"sending/auto_send.py"}
    assert "sending/auto_send.py" not in writers["approved_by_user_at"]


def test_scheduler_never_imports_sending() -> None:
    scheduler = (SRC / "scheduling/scheduler.py").read_text(encoding="utf-8")
    assert "scribe.sending" not in scheduler and "send_text" not in scheduler
