"""DraftOutput (pydantic) must stay equivalent to contracts/draft-output.schema.json (T012)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from scribe.llm.provider import DraftOutput

SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "specs/001-telegram-reply-agent/contracts/draft-output.schema.json"
)


def _inline_refs(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            target = defs[node["$ref"].rsplit("/", 1)[-1]]
            merged = {
                **_inline_refs(target, defs),
                **{k: v for k, v in node.items() if k != "$ref"},
            }
            return merged
        return {k: _inline_refs(v, defs) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline_refs(v, defs) for v in node]
    return node


def _normalize(node: Any, *, root: bool = False) -> Any:
    """Canonical form: no titles/$schema, nullable unions as type lists, sorted ``required``."""
    if isinstance(node, list):
        return [_normalize(v) for v in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in {"title", "$schema", "$defs"}:
            continue
        if root and key == "description":
            continue
        out[key] = _normalize(value)
    any_of = out.get("anyOf")
    if isinstance(any_of, list) and all(set(v) == {"type"} for v in any_of):
        out.pop("anyOf")
        out["type"] = [v["type"] for v in any_of]
    if isinstance(out.get("type"), list):
        out["type"] = sorted(out["type"])
    if isinstance(out.get("required"), list):
        out["required"] = sorted(out["required"])
    return out


def test_draft_output_schema_matches_contract() -> None:
    contract = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    generated = DraftOutput.model_json_schema()
    generated = _inline_refs(generated, generated.get("$defs", {}))

    assert _normalize(generated, root=True) == _normalize(contract, root=True)
    # The model docstring doubles as the schema description.
    assert generated["description"] == contract["description"]


def _valid_payload() -> dict[str, Any]:
    return {
        "text": "Synthetic reply, ready by [[date]].",
        "language": "en",
        "needs_info": True,
        "placeholders": [{"marker": "[[date]]", "question": "When will it be ready?"}],
        "sources": [{"kind": "message", "id": "msg:1", "used_for": "the request"}],
        "flags": {
            "creates_commitment": {"value": False, "what": None, "due": None},
            "sensitive": {"value": False, "categories": []},
        },
        "conflicts": [],
        "tone_shift_applied": "none",
        "context_warnings": ["tasks_unavailable"],
    }


def test_valid_payload_parses() -> None:
    output = DraftOutput.model_validate(_valid_payload())
    assert output.placeholders[0].marker == "[[date]]"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("sources"),
        lambda p: p.update(extra_field=1),
        lambda p: p.update(tone_shift_applied="sarcastic"),
        lambda p: p["flags"]["sensitive"].update(categories=["weather"]),
        lambda p: p["sources"][0].update(kind="guess"),
    ],
)
def test_invalid_payloads_are_rejected(mutate: Any) -> None:
    payload = _valid_payload()
    mutate(payload)
    with pytest.raises(ValidationError):
        DraftOutput.model_validate(payload)
