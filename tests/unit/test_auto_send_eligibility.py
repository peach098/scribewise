"""specs/014 SC-001: ``eligibility()`` names every reason a draft may not be sent on its own.
Table-driven over plain objects, no database. Synthetic texts only."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from typing import Any

import pytest

from scribe.db.models import Draft, User, UserState, WorkChat
from scribe.llm.provider import DraftOutput
from scribe.sending.auto_send import (
    AUTO_SEND_ALLOWED_WARNINGS,
    BlockReason,
    Eligibility,
    eligibility,
)

WEDNESDAY_NOON = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
WEDNESDAY_NIGHT = datetime(2026, 10, 14, 23, 0, tzinfo=UTC)


def _output(**overrides: Any) -> DraftOutput:
    payload: dict[str, Any] = {
        "text": "Synthetic reply",
        "language": "en",
        "needs_info": False,
        "placeholders": [],
        "sources": [],
        "flags": {
            "creates_commitment": {"value": False, "what": None, "due": None},
            "sensitive": {"value": False, "categories": []},
        },
        "conflicts": [],
        "tone_shift_applied": "none",
        "context_warnings": [],
    }
    payload.update(overrides)
    return DraftOutput.model_validate(payload)


@dataclass
class Case:
    """One call of ``eligibility`` as the arm and fire paths make it."""

    output: DraftOutput | None = field(default_factory=_output)
    system_warnings: list[str] = field(default_factory=list)
    context_warnings: list[str] = field(default_factory=list)
    draft: dict[str, Any] = field(default_factory=dict)
    chat: dict[str, Any] = field(default_factory=dict)
    user: dict[str, Any] = field(default_factory=dict)
    fire_at: datetime | None = None

    def run(self) -> Eligibility:
        draft = Draft(version=1, text="Synthetic reply", outdated=False, possibly_unnecessary=False)
        for key, value in self.draft.items():
            setattr(draft, key, value)
        chat = WorkChat(auto_send=True, enabled=True, excluded=False)
        for key, value in self.chat.items():
            setattr(chat, key, value)
        user = User(
            state=UserState.active,
            timezone="UTC",
            working_days=[0, 1, 2, 3, 4],
            work_start=time(9, 0),
            work_end=time(18, 0),
            access_suspended_at=None,
        )
        for key, value in self.user.items():
            setattr(user, key, value)
        return eligibility(
            draft=draft,
            output=self.output,
            system_warnings=self.system_warnings,
            context_warnings=self.context_warnings,
            chat=chat,
            user=user,
            fire_at=self.fire_at,
        )


def _flags(**kinds: Any) -> DraftOutput:
    base = _output().flags.model_dump()
    for key, value in kinds.items():
        base[key] = value
    return _output(flags=base)


CASES: dict[str, tuple[Case, set[BlockReason]]] = {
    "clean": (Case(), set()),
    "injection": (Case(system_warnings=["suspected_injection"]), {BlockReason.hard_stop}),
    "action_claim": (Case(system_warnings=["action_claim"]), {BlockReason.hard_stop}),
    "commitment": (
        Case(output=_flags(creates_commitment={"value": True, "what": "send it", "due": None})),
        {BlockReason.commitment},
    ),
    "sensitive": (
        Case(output=_flags(sensitive={"value": True, "categories": ["payment"]})),
        {BlockReason.sensitive},
    ),
    "needs_info": (Case(output=_output(needs_info=True)), {BlockReason.placeholders}),
    "marker_in_text": (
        Case(draft={"text": "Synthetic [[question]] reply"}),
        {BlockReason.placeholders},
    ),
    "dates_differ": (
        Case(system_warnings=['date_mismatch:{"key":null,"parts":[]}']),
        {BlockReason.dates_differ},
    ),
    "conflict": (
        Case(
            output=_output(
                conflicts=[{"with_kind": "task", "with_id": "task:1", "description": "Synthetic"}]
            )
        ),
        {BlockReason.conflict},
    ),
    "weak_style_system": (Case(system_warnings=["weak_style_profile"]), {BlockReason.weak_style}),
    "weak_style_context": (Case(context_warnings=["weak_style_profile"]), {BlockReason.weak_style}),
    "ooo_warning": (Case(system_warnings=["out_of_office"]), {BlockReason.out_of_office}),
    "ooo_user": (Case(user={"state": UserState.out_of_office}), {BlockReason.out_of_office}),
    "paused": (Case(user={"state": UserState.paused}), {BlockReason.paused}),
    "setting_up": (Case(user={"state": UserState.setting_up}), {BlockReason.paused}),
    "habit_warning": (Case(system_warnings=["drafted_by_habit"]), {BlockReason.warning}),
    "unknown_warning": (Case(system_warnings=["something_new"]), {BlockReason.warning}),
    "context_warning": (Case(context_warnings=["tasks_unavailable"]), {BlockReason.warning}),
    "no_output": (Case(output=None), {BlockReason.no_output}),
    "outdated": (Case(draft={"outdated": True}), {BlockReason.outdated}),
    "unnecessary": (
        Case(draft={"possibly_unnecessary": True}),
        {BlockReason.possibly_unnecessary},
    ),
    "chat_auto_send_off": (Case(chat={"auto_send": False}), {BlockReason.chat_off}),
    "chat_disabled": (Case(chat={"enabled": False}), {BlockReason.chat_off}),
    "chat_excluded": (Case(chat={"excluded": True}), {BlockReason.chat_off}),
    "suspended": (Case(user={"access_suspended_at": WEDNESDAY_NOON}), {BlockReason.suspended}),
    "version_two": (Case(draft={"version": 2}), {BlockReason.not_first_version}),
    "filled_version": (
        Case(draft={"edited_text": "Synthetic reply"}),
        {BlockReason.not_first_version},
    ),
    "outside_hours": (Case(fire_at=WEDNESDAY_NIGHT), {BlockReason.outside_hours}),
    "inside_hours": (Case(fire_at=WEDNESDAY_NOON), set()),
    "hours_not_checked_at_arm": (Case(fire_at=None, user={"work_end": time(10, 0)}), set()),
    "several_faults": (
        Case(
            system_warnings=["suspected_injection", "drafted_on_request"],
            output=_output(needs_info=True),
            draft={"outdated": True, "version": 3},
            chat={"auto_send": False},
        ),
        {
            BlockReason.hard_stop,
            BlockReason.warning,
            BlockReason.placeholders,
            BlockReason.outdated,
            BlockReason.not_first_version,
            BlockReason.chat_off,
        },
    ),
}


@pytest.mark.parametrize("name", CASES)
def test_each_fault_yields_exactly_its_reasons(name: str) -> None:
    case, expected = CASES[name]
    result = case.run()
    assert set(result.reasons) == expected
    assert result.eligible is (not expected)
    assert len(result.reasons) == len(set(result.reasons))  # no duplicates


def test_clean_first_version_is_eligible() -> None:
    assert Case(fire_at=WEDNESDAY_NOON).run() == Eligibility(reasons=())


def test_every_block_reason_is_covered_by_a_case() -> None:
    covered = {reason for _, expected in CASES.values() for reason in expected}
    assert covered == set(BlockReason)


def test_no_warning_is_allowed_in_this_release() -> None:
    assert frozenset() == AUTO_SEND_ALLOWED_WARNINGS


def test_a_listed_warning_stops_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    from scribe.sending import auto_send

    monkeypatch.setattr(auto_send, "AUTO_SEND_ALLOWED_WARNINGS", frozenset({"drafted_by_habit"}))
    assert Case(system_warnings=["drafted_by_habit"]).run().eligible
    assert not Case(system_warnings=["drafted_on_request"]).run().eligible


def test_fails_closed_and_never_raises_on_broken_input() -> None:
    broken: Callable[[], Eligibility] = Case(
        fire_at=WEDNESDAY_NOON, user={"timezone": "Not/AZone"}
    ).run
    result = broken()
    assert not result.eligible
