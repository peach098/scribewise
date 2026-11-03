"""Third-party DEBUG output never reaches the logs unless explicitly opted in (issue #55).

The anthropic SDK formats the whole request (the prompt) into a DEBUG message string, where
key-based redaction cannot see it. All values below are synthetic.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator

import pytest
import structlog

from scribe.config import Settings
from scribe.logging import CONTENT_LOGGERS, CREDENTIAL_LOGGERS, configure_logging

PROMPT = "SYNTHETIC-PROMPT-CONTENT"


@pytest.fixture
def stream() -> Iterator[io.StringIO]:
    out = io.StringIO()
    yield out
    structlog.reset_defaults()
    logging.getLogger().handlers.clear()
    for name in (*CONTENT_LOGGERS, *CREDENTIAL_LOGGERS):
        logging.getLogger(name).setLevel(logging.NOTSET)


@pytest.mark.parametrize(
    "logger_name",
    ["anthropic._base_client", "openai._base_client", "telethon.network.mtprotosender", "aiogram"],
)
def test_library_debug_records_are_dropped_at_app_debug(
    stream: io.StringIO, logger_name: str
) -> None:
    configure_logging("DEBUG", stream=stream)

    logging.getLogger(logger_name).debug("Request options: %s", {"messages": [PROMPT]})
    logging.getLogger(logger_name).info("library info")
    structlog.get_logger("scribe.test").debug("app_debug_event")

    out = stream.getvalue()
    assert PROMPT not in out
    assert "library info" in out
    assert "app_debug_event" in out  # the app itself still logs at DEBUG


def test_a_library_raising_its_own_level_is_still_filtered(stream: io.StringIO) -> None:
    configure_logging("DEBUG", stream=stream)
    logging.getLogger("anthropic").setLevel(logging.DEBUG)  # what ANTHROPIC_LOG=debug does

    logging.getLogger("anthropic._base_client").debug("Request options: %s", PROMPT)

    assert PROMPT not in stream.getvalue()


def test_opt_in_lets_library_debug_through(stream: io.StringIO) -> None:
    configure_logging("DEBUG", stream=stream, libraries_debug=True)

    logging.getLogger("anthropic._base_client").debug("Request options: %s", PROMPT)

    assert PROMPT in stream.getvalue()


def test_http_client_loggers_stay_at_warning_even_with_opt_in(stream: io.StringIO) -> None:
    configure_logging("DEBUG", stream=stream, libraries_debug=True)

    logging.getLogger("httpx").info("HTTP Request: GET https://x.invalid/?token=%s", PROMPT)

    assert PROMPT not in stream.getvalue()


def test_library_info_follows_a_stricter_app_level(stream: io.StringIO) -> None:
    configure_logging("WARNING", stream=stream)

    logging.getLogger("aiogram.event").info("Update handled")

    assert stream.getvalue() == ""


@pytest.mark.parametrize("libraries_debug", [False, True])
def test_real_anthropic_sdk_request_log(stream: io.StringIO, libraries_debug: bool) -> None:
    """The SDK's own DEBUG "Request options" line carries the prompt; it is dropped by default."""
    from anthropic import Anthropic
    from anthropic._models import FinalRequestOptions

    configure_logging("DEBUG", stream=stream, libraries_debug=libraries_debug)
    client = Anthropic(api_key="synthetic-key")
    options = FinalRequestOptions.construct(
        method="post",
        url="/v1/messages",
        json_data={"messages": [{"role": "user", "content": PROMPT}]},
    )

    client._build_request(options)

    assert (PROMPT in stream.getvalue()) is libraries_debug


def test_setting_defaults_to_off(settings: Settings) -> None:
    assert settings.log_libraries_debug is False
