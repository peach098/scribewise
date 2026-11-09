"""Invoice payload format (specs/015 research P2): ``sub1:<offer uuid>``."""

from __future__ import annotations

import uuid

import pytest

from scribe.payments.offers import make_payload, parse_payload


def test_round_trip_and_size() -> None:
    offer_id = uuid.uuid4()
    payload = make_payload(offer_id)
    assert payload == f"sub1:{offer_id}"
    assert len(payload.encode()) <= 128
    assert parse_payload(payload) == offer_id


@pytest.mark.parametrize(
    "payload",
    [
        "sub1:",
        "sub1:not-a-uuid",
        f"sub2:{uuid.uuid4()}",
        "",
        "x" * 129,
        f" sub1:{uuid.uuid4()}",
        str(uuid.uuid4()),
    ],
)
def test_foreign_payloads_do_not_parse(payload: str) -> None:
    assert parse_payload(payload) is None
