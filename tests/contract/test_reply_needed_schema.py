"""ReplyNeededOutput (pydantic) must stay equivalent to
specs/004-skip-closing-messages/contracts/reply-needed-output.schema.json (004 T009)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from scribe.llm.provider import ReplyNeededOutput

SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "specs/004-skip-closing-messages/contracts/reply-needed-output.schema.json"
)


def _shape(schema: dict[str, Any]) -> dict[str, Any]:
    """What must match: required fields, property types, enum values, bounds, no extra keys."""
    keep = ("type", "enum", "minimum", "maximum")
    return {
        "required": sorted(schema["required"]),
        "additionalProperties": schema.get("additionalProperties"),
        "properties": {
            name: {k: v for k, v in prop.items() if k in keep}
            for name, prop in schema["properties"].items()
        },
    }


def test_reply_needed_schema_matches_contract() -> None:
    contract = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    generated = ReplyNeededOutput.model_json_schema()
    assert _shape(generated) == _shape(contract)
    assert set(generated["properties"]) == set(contract["properties"])


def _payload() -> dict[str, Any]:
    return {
        "reply_needed": False,
        "category": "thanks",
        "confidence": 0.9,
        "resolves_pending": False,
    }


def test_valid_payload_parses() -> None:
    assert ReplyNeededOutput.model_validate(_payload()).category == "thanks"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("resolves_pending"),
        lambda p: p.update(extra_field=1),
        lambda p: p.update(category="greeting"),
        lambda p: p.update(confidence=1.2),
        lambda p: p.update(confidence=-0.1),
    ],
)
def test_invalid_payloads_are_rejected(mutate: Any) -> None:
    payload = _payload()
    mutate(payload)
    with pytest.raises(ValidationError):
        ReplyNeededOutput.model_validate(payload)
