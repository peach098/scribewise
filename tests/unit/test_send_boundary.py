"""Principle I, statically: ``sending/send.py`` is the only caller of ``send_text`` and is reached
only from the review module's approve/edit path."""

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


def test_send_approved_draft_only_called_from_review_approval_path() -> None:
    assert set(_calls("send_approved_draft")) == {"telegram/control_bot/review.py"}
    review = (SRC / "telegram/control_bot/review.py").read_text(encoding="utf-8")
    tree = ast.parse(review)
    owners = {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.AsyncFunctionDef)
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "send_approved_draft"
    }
    assert owners == {"_approve_and_send"}
    helper_callers = {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.AsyncFunctionDef)
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_approve_and_send"
    }
    assert helper_callers == {"on_approve", "on_edit", "on_edited_text"}


def test_scheduler_never_imports_sending() -> None:
    scheduler = (SRC / "scheduling/scheduler.py").read_text(encoding="utf-8")
    assert "scribe.sending" not in scheduler and "send_text" not in scheduler
