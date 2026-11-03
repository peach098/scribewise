"""The redaction processor must drop content-bearing keys at any depth (Principle II).

All values below are synthetic.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
import structlog

from scribe.logging import REDACTED_KEYS, configure_logging, redact, redact_processor

SECRET = "SYNTHETIC-SECRET-CONTENT"


def _all_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for k, v in value.items():
            keys.add(k)
            keys |= _all_keys(v)
    elif isinstance(value, list | tuple):
        for v in value:
            keys |= _all_keys(v)
    return keys


def _nested_event() -> dict[str, Any]:
    return {
        "event": "draft_prepared",
        "draft_id": 42,
        "text": SECRET,
        "body": SECRET,
        "meta": {
            "edited_text": SECRET,
            "deep": {"session_string": SECRET, "level3": [{"credentials": {"token": SECRET}}]},
        },
        "profiles": [{"id": 1, "examples": [SECRET, SECRET]}, ({"Text": SECRET},)],
        "route": "compose.draft",
    }


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)
    yield stream
    structlog.reset_defaults()
    logging.getLogger().handlers.clear()


def test_redacted_keys_cover_contract() -> None:
    assert {"text", "body", "edited_text", "session_string", "credentials", "examples"} <= (
        REDACTED_KEYS
    )


def test_jira_secrets_are_redacted() -> None:
    event = {
        "event": "x",
        "config": {"pinned_cert": {"pem": "-----BEGIN"}, "site_url": "https://jira.example.com"},
        "api_token": "t",
        "pat": "p",
        "email": "me@example.com",
    }
    assert redact(event) == {"event": "x", "config": {"site_url": "https://jira.example.com"}}


def test_no_redacted_key_survives_at_any_depth() -> None:
    result = redact_processor(None, "info", _nested_event())

    keys = {k.lower() for k in _all_keys(result)}
    assert keys.isdisjoint(REDACTED_KEYS)
    assert SECRET not in json.dumps(result)


def test_non_sensitive_fields_are_kept() -> None:
    result = redact(_nested_event())

    assert result["event"] == "draft_prepared"
    assert result["draft_id"] == 42
    assert result["route"] == "compose.draft"
    assert result["meta"]["deep"]["level3"] == [{}]
    assert result["profiles"][0] == {"id": 1}


def test_input_is_not_mutated() -> None:
    event = _nested_event()
    redact(event)
    assert event["text"] == SECRET
    assert event["meta"]["deep"]["session_string"] == SECRET


def test_structlog_output_is_redacted_json(log_stream: io.StringIO) -> None:
    structlog.get_logger("scribe.test").info(
        "draft_prepared", draft_id=7, text=SECRET, payload={"body": SECRET, "ok": True}
    )

    line = log_stream.getvalue().strip()
    record = json.loads(line)
    assert record["event"] == "draft_prepared"
    assert record["draft_id"] == 7
    assert record["payload"] == {"ok": True}
    assert '"text"' not in line
    assert SECRET not in line


def test_stdlib_logger_output_is_redacted(log_stream: io.StringIO) -> None:
    logging.getLogger("thirdparty").warning(
        "library message", extra={"text": SECRET, "credentials": SECRET, "chat_id": 5}
    )

    line = log_stream.getvalue().strip()
    record = json.loads(line)
    assert record["event"] == "library message"
    assert record["chat_id"] == 5
    assert SECRET not in line
