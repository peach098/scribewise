"""Usage limits per user (docs/plugins.md): the default resolver gives the deployment caps and
no other limit without a query; a plugin's resolver decides the caps that ``BudgetGuard``,
``paused_now`` and ``budget_level`` apply, and the draft-count limit. No database: the session
and the spend query are fakes."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest

from scribe.config import Settings
from scribe.db.models import User
from scribe.db.repos import llm_spend as llm_spend_repo
from scribe.drafting import budget
from scribe.drafting.budget import (
    BudgetCaps,
    BudgetGuard,
    SettingsLimitsResolver,
    UsageLimits,
    budget_level,
    level_for,
    paused_now,
    user_budget_level,
    user_limits,
)
from scribe.observability.spend_alert import build_alert
from tests.conftest import FROZEN_NOW

USER_ID = uuid.UUID(int=1)


class FakeSession:
    """Only what the budget code calls: ``get(User, id)``."""

    def __init__(self, user: User | None) -> None:
        self.user = user
        self.gets = 0

    async def get(self, model: Any, key: Any) -> User | None:
        self.gets += 1
        return self.user


class FakeFactory:
    def __init__(self, session: FakeSession) -> None:
        self.session = session
        self.opened = 0

    def __call__(self) -> Any:
        @asynccontextmanager
        async def context() -> AsyncIterator[FakeSession]:
            self.opened += 1
            yield self.session

        return context()


class Fixed:
    """A plugin-style resolver with the same limits for every user."""

    def __init__(self, limits: UsageLimits) -> None:
        self.limits = limits
        self.asked: list[uuid.UUID] = []

    async def limits_for(self, session: Any, user: User, now: datetime) -> UsageLimits:
        self.asked.append(user.id)
        return self.limits


@pytest.fixture
def spent(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Today's spend the fake query returns, and what it was asked."""
    state: dict[str, Any] = {"amount": Decimal("0"), "excluded": None}

    async def spent_on(
        session: Any, user_id: uuid.UUID, day: date, *, exclude_routes: Any = ()
    ) -> Decimal:
        state["excluded"] = frozenset(exclude_routes)
        return Decimal(state["amount"])

    monkeypatch.setattr(llm_spend_repo, "spent_on", spent_on)
    return state


def _user() -> User:
    return User(id=USER_ID, timezone="UTC", language="en")


def test_settings_resolver_gives_the_deployment_caps_and_nothing_else(settings: Settings) -> None:
    configured = settings.model_copy(
        update={
            "llm_daily_soft_cap_usd": 0.5,
            "llm_daily_hard_cap_usd": 2.0,
            "llm_budget_excluded_routes": ["profile.build"],
            "profile_auto_refresh_default": False,
        }
    )
    limits = SettingsLimitsResolver.from_settings(configured).limits
    assert limits == UsageLimits(
        caps=BudgetCaps(0.5, 2.0, frozenset({"profile.build"})),
        max_drafts_per_day=None,
        max_work_chats=None,
        profile_refresh_allowed=True,
        profile_refresh_default=False,
    )


async def test_default_resolver_needs_no_query(settings: Settings) -> None:
    session = FakeSession(_user())
    resolver = SettingsLimitsResolver.from_settings(settings)
    limits = await user_limits(session, USER_ID, resolver, FROZEN_NOW)  # type: ignore[arg-type]
    assert limits.caps == BudgetCaps.from_settings(settings)
    assert session.gets == 0


async def test_custom_caps_decide_budget_level(spent: dict[str, Any]) -> None:
    session = FakeSession(_user())
    tight = Fixed(UsageLimits(caps=BudgetCaps(0.05, 0.10)))
    spent["amount"] = Decimal("0.07")

    level = await user_budget_level(session, USER_ID, tight, FROZEN_NOW)  # type: ignore[arg-type]

    assert level == "soft"
    assert tight.asked == [USER_ID]
    default = SettingsLimitsResolver(BudgetCaps(1.0, 3.0))
    assert await user_budget_level(session, USER_ID, default, FROZEN_NOW) == "ok"  # type: ignore[arg-type]


async def test_budget_level_skips_the_excluded_routes(spent: dict[str, Any]) -> None:
    caps = BudgetCaps(0.1, 0.1, frozenset({"profile.build"}))
    spent["amount"] = Decimal("0.2")
    assert await budget_level(None, USER_ID, caps, FROZEN_NOW) == "hard"  # type: ignore[arg-type]
    assert spent["excluded"] == frozenset({"profile.build"})


async def test_custom_caps_decide_guard_level_and_paused_now(
    settings: Settings, spent: dict[str, Any]
) -> None:
    factory = FakeFactory(FakeSession(_user()))
    tight = Fixed(UsageLimits(caps=BudgetCaps(0.05, 0.10)))
    spent["amount"] = Decimal("0.10")

    guard = BudgetGuard(factory, tight, clock=lambda: FROZEN_NOW)  # type: ignore[arg-type]
    assert await guard.level(USER_ID) == "hard"
    assert await guard.pause_reason(USER_ID) == "cap"
    assert await paused_now(factory, settings, USER_ID, FROZEN_NOW, tight)  # type: ignore[arg-type]
    # The deployment caps ($1 / $3) would not pause at $0.10.
    assert not await paused_now(factory, settings, USER_ID, FROZEN_NOW)  # type: ignore[arg-type]


async def test_default_guard_with_caps_off_opens_no_session() -> None:
    factory = FakeFactory(FakeSession(_user()))
    off = SettingsLimitsResolver(BudgetCaps(0, 0))
    assert await level_for(factory, USER_ID, off, FROZEN_NOW) == "ok"  # type: ignore[arg-type]
    guard = BudgetGuard(factory, off)  # type: ignore[arg-type]
    assert await guard.pause_reason(USER_ID) is None
    assert factory.opened == 0


async def test_draft_limit_pauses_with_its_own_reason(
    spent: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = FakeFactory(FakeSession(_user()))
    drafts = {"today": 2}

    async def drafts_today(session: Any, user_id: uuid.UUID, now: datetime) -> int:
        return drafts["today"]

    monkeypatch.setattr(budget, "drafts_today", drafts_today)
    limited = Fixed(UsageLimits(caps=BudgetCaps(1.0, 3.0), max_drafts_per_day=3))
    guard = BudgetGuard(factory, limited, clock=lambda: FROZEN_NOW)  # type: ignore[arg-type]

    assert await guard.pause_reason(USER_ID) is None
    drafts["today"] = 3
    assert await guard.pause_reason(USER_ID) == "drafts"
    assert await guard.paused(USER_ID)
    spent["amount"] = Decimal("3")  # the cap wins when both apply
    assert await guard.pause_reason(USER_ID) == "cap"


async def test_a_missing_user_has_no_caps() -> None:
    session = FakeSession(None)
    limits = await user_limits(
        session,  # type: ignore[arg-type]
        USER_ID,
        Fixed(UsageLimits(caps=BudgetCaps(0.05, 0.10))),
        FROZEN_NOW,
    )
    assert not limits.caps.enabled


def test_spend_alert_uses_each_users_caps_and_counted_spend() -> None:
    small, large = uuid.UUID(int=1), uuid.UUID(int=2)
    totals = {small: Decimal("0.5"), large: Decimal("0.5")}
    deployment = BudgetCaps(1.0, 3.0)

    plain = build_alert(FROZEN_NOW.date(), totals, deployment)
    assert plain.soft == [] and plain.hard == []

    own = build_alert(
        FROZEN_NOW.date(),
        totals,
        deployment,
        user_caps={small: BudgetCaps(0.1, 0.2)},
        counted={small: Decimal("0.5"), large: Decimal("0.5")},
    )
    assert own.hard == [(small, Decimal("0.5"))]
    excluded = build_alert(
        FROZEN_NOW.date(),
        totals,
        deployment,
        user_caps={small: BudgetCaps(0.1, 0.2)},
        counted={small: Decimal("0"), large: Decimal("0.5")},
    )
    assert excluded.hard == [] and excluded.soft == []
