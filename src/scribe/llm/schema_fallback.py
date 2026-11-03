"""Structured output for providers without a native JSON-schema mode.

The JSON schema goes into the system prompt; the reply is parsed and validated; on failure the
model gets one corrective turn (validation error *locations and types only*, never values);
a second failure raises ``ProviderOutputInvalid`` (with the usage of every attempt, for the log).
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, ValidationError

from scribe.llm.provider import ProviderOutputInvalid, Usage

ChatMessage = dict[str, str]
# (system, messages) -> (reply text, usage)
CompleteText = Callable[[str, list[ChatMessage]], Awaitable[tuple[str, Usage]]]

_FENCE = re.compile(r"```(?:json)?\s*(?P<body>.*?)```", re.DOTALL)

RETRY_MESSAGE = (
    "Your previous reply did not match the required JSON schema ({errors}). "
    "Reply again with only the corrected JSON object, no other text."
)


def schema_instructions(schema: type[BaseModel]) -> str:
    return (
        "Respond with a single JSON object that validates against this JSON schema. "
        "Output only the JSON object: no prose, no markdown.\n"
        f"{json.dumps(schema.model_json_schema(), ensure_ascii=False, sort_keys=True)}"
    )


def extract_json(text: str) -> str:
    """The JSON object inside ``text`` (tolerates code fences and surrounding prose)."""
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group("body")
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object in reply")
    return text[start : end + 1]


def error_summary(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = [
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['type']}"
            for err in exc.errors()[:10]
        ]
        return "; ".join(parts)
    return "reply was not a JSON object"


async def complete_with_schema_fallback[T: BaseModel](
    complete: CompleteText,
    *,
    system: str,
    user_text: str,
    schema: type[T],
    max_retries: int = 1,
) -> tuple[T, Usage]:
    full_system = f"{system}\n\n{schema_instructions(schema)}"
    messages: list[ChatMessage] = [{"role": "user", "content": user_text}]
    total = Usage()
    for attempt in range(max_retries + 1):
        reply, usage = await complete(full_system, messages)
        total = total + usage
        try:
            return schema.model_validate_json(extract_json(reply)), total
        except (ValidationError, ValueError) as exc:
            summary = error_summary(exc)
        if attempt < max_retries:
            messages = [
                *messages,
                {"role": "assistant", "content": reply},
                {"role": "user", "content": RETRY_MESSAGE.format(errors=summary)},
            ]
    raise ProviderOutputInvalid(
        f"{schema.__name__} invalid after {max_retries + 1} attempts ({summary})", usage=total
    )
