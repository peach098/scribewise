"""Structured JSON logging with content redaction (constitution Principles II and VI).

Every log line, from structlog or from stdlib loggers used by libraries, passes through
``redact_processor`` immediately before rendering. Keys that may carry message content or
credentials are dropped at any nesting depth, so they can never reach the log sink.

Key-based redaction cannot see text that a library formats into the message string itself:
at DEBUG the anthropic SDK logs ``Request options: ...`` with the whole ``messages`` payload,
and Telethon logs raw TL objects. Third-party loggers therefore have a floor
(``LIBRARY_LEVEL_FLOORS``) that holds whatever ``SCRIBE_LOG_LEVEL`` says (issue #55). The floor
is applied twice: as the logger's level, and as a filter on our handler, so a library that
raises its own logger level (``ANTHROPIC_LOG=debug``) still cannot reach the sink. Only the
explicit ``SCRIBE_LOG_LIBRARIES_DEBUG=true`` opt-in lifts the content floor; it writes message
content to the logs. HTTP client loggers stay at WARNING even then, because request URLs can
carry credentials (Trello key and token).
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping, MutableMapping
from typing import IO, Any

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

REDACTED_KEYS: frozenset[str] = frozenset(
    {
        "text",
        "body",
        "edited_text",
        "session_string",
        "session_token",  # telegram_sessions.session_string, sealed with its owner (#59)
        "master_key",
        "master_keys_previous",
        "credentials",
        "examples",
        # Issue #46: personal data, never logged
        "grammatical_gender",
        # Jira (specs/003-jira-commitments): tokens, the account email, trusted certificates
        "api_token",
        "pat",
        "email",
        "pem",
        "pinned_cert",
    }
)


# Loggers whose DEBUG output can carry message content: INFO-or-higher unless opted in.
CONTENT_LOGGER_FLOOR = logging.INFO
CONTENT_LOGGERS: tuple[str, ...] = (
    "anthropic",  # DEBUG: "Request options: ..." with the full prompt
    "openai",  # DEBUG: request metadata today; same SDK generator as anthropic
    "telethon",  # DEBUG: TL objects (updates, auth key messages) formatted into the message
    "aiogram",  # DEBUG: update and request details
    "aiohttp",
    "asyncio",  # debug mode: reprs of slow callbacks and their arguments
    "asyncpg",
)
# Loggers whose INFO output can carry credentials (request URLs): WARNING, never lifted.
CREDENTIAL_LOGGER_FLOOR = logging.WARNING
CREDENTIAL_LOGGERS: tuple[str, ...] = ("httpx", "httpcore", "httpx2", "httpcore2", "urllib3")


def library_level_floors(*, libraries_debug: bool = False) -> dict[str, int]:
    """Minimum level per third-party logger namespace (the logger and all its children)."""
    floors = {name: CREDENTIAL_LOGGER_FLOOR for name in CREDENTIAL_LOGGERS}
    if not libraries_debug:
        floors.update({name: CONTENT_LOGGER_FLOOR for name in CONTENT_LOGGERS})
    return floors


class LibraryLevelFilter(logging.Filter):
    """Drop records from a floored library namespace that are below that namespace's floor."""

    def __init__(self, floors: Mapping[str, int]) -> None:
        super().__init__()
        self._floors = dict(floors)

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name
        while True:
            floor = self._floors.get(name)
            if floor is not None:
                return record.levelno >= floor
            dot = name.rfind(".")
            if dot < 0:
                return True
            name = name[:dot]


def redact(value: Any) -> Any:
    """Return a copy of ``value`` with every ``REDACTED_KEYS`` entry removed at any depth."""
    if isinstance(value, Mapping):
        return {
            k: redact(v)
            for k, v in value.items()
            if not (isinstance(k, str) and k.lower() in REDACTED_KEYS)
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact(v) for v in value)
    if isinstance(value, set | frozenset):
        return [redact(v) for v in value]
    return value


def redact_processor(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    """structlog processor: drop content-bearing keys anywhere in the event."""
    redacted: MutableMapping[str, Any] = redact(event_dict)
    return dict(redacted)


def configure_logging(
    level: str = "INFO", stream: IO[str] | None = None, *, libraries_debug: bool = False
) -> None:
    """Configure structlog and the stdlib root logger to emit redacted JSON lines.

    ``libraries_debug`` (``SCRIBE_LOG_LIBRARIES_DEBUG``) lets third-party content loggers follow
    ``level``; it writes message content to the logs and is off by default.
    """
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[*shared, structlog.stdlib.ExtraAdder()],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            redact_processor,  # last step before rendering: nothing bypasses it
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
    )

    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(formatter)
    floors = library_level_floors(libraries_debug=libraries_debug)
    handler.addFilter(LibraryLevelFilter(floors))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    for name in CONTENT_LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET)  # undo a previous floor on opt-in
    for name, floor in floors.items():
        # never below the app level either: a library logger's own level overrides the root's
        logging.getLogger(name).setLevel(max(floor, root.level))


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    return logger
