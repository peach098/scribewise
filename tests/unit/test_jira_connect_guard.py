"""Issue #61: Jira connection attempts are rate limited per user, and network-level failures
reach the user as one message (no port-scan oracle). Synthetic values only."""

from __future__ import annotations

import pytest

from scribe.context.tasks.base import (
    JiraAddressInvalid,
    JiraAuthFailed,
    JiraCertificateUntrusted,
    JiraPortNotAllowed,
    JiraUnreachable,
    JiraVersionUnsupported,
    TaskSourceUnavailable,
)
from scribe.context.tasks.jira.attempts import AttemptLimiter
from scribe.i18n import t
from scribe.telegram.control_bot.jira_connect import error_text

# --- rate limit -----------------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_limiter_allows_up_to_the_limit_then_waits() -> None:
    clock = _Clock()
    limiter = AttemptLimiter(max_attempts=3, window_s=600, monotonic=clock)
    assert [limiter.try_attempt("u1") for _ in range(3)] == [0.0, 0.0, 0.0]
    assert limiter.try_attempt("u1") == pytest.approx(600)
    clock.now += 100
    assert limiter.try_attempt("u1") == pytest.approx(500)  # a refused try is not counted
    assert limiter.try_attempt("u2") == 0.0  # per user


def test_limiter_window_slides() -> None:
    clock = _Clock()
    limiter = AttemptLimiter(max_attempts=2, window_s=600, monotonic=clock)
    limiter.try_attempt("u1")
    clock.now += 300
    limiter.try_attempt("u1")
    assert limiter.try_attempt("u1") > 0
    clock.now += 300  # the first attempt left the window
    assert limiter.try_attempt("u1") == 0.0
    assert limiter.try_attempt("u1") == pytest.approx(300)


def test_limiter_forgets_idle_users() -> None:
    clock = _Clock()
    limiter = AttemptLimiter(max_attempts=2, window_s=60, monotonic=clock)
    for user in range(100):
        limiter.try_attempt(user)
    clock.now += 61
    limiter.try_attempt("other")
    assert len(limiter) == 1


def test_limiter_defaults() -> None:
    limiter = AttemptLimiter()
    assert (limiter.max_attempts, limiter.window_s) == (5, 600)


# --- one message for network-level failures -------------------------------------------------------

NETWORK_FAILURES: list[TaskSourceUnavailable] = [
    JiraUnreachable("could not resolve the jira host"),
    JiraUnreachable("jira did not answer (ConnectError)"),
    JiraUnreachable("private", blocked=True),
    JiraCertificateUntrusted("the jira server certificate is not trusted"),
]


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_network_failures_share_one_message(lang: str) -> None:
    texts = {error_text(exc, lang) for exc in NETWORK_FAILURES}
    assert texts == {t("jira.unreachable", lang)}


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_answers_after_a_connection_keep_their_own_message(lang: str) -> None:
    network = t("jira.unreachable", lang)
    others = [
        JiraAddressInvalid("bad"),
        JiraPortNotAllowed((443, 8443)),
        JiraAuthFailed("no"),
        JiraVersionUnsupported("9.0", "10.3"),
        TaskSourceUnavailable("this address is not a jira server"),
    ]
    texts = [error_text(exc, lang) for exc in others]
    assert network not in texts
    assert len(set(texts)) == len(texts)


def test_port_message_lists_the_allowed_ports() -> None:
    assert "443, 8443" in error_text(JiraPortNotAllowed((443, 8443)), "en")
    assert "9443" in error_text(JiraPortNotAllowed((9443,)), "ru")
