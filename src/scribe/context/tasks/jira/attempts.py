"""Per-user limit on Jira connection attempts (issue #61).

Each attempt makes the server open a connection to an address the user typed, so without a
limit the connect flow could be used to probe hosts with the server's IP as the source. The
limit is a sliding window kept in memory: it resets on restart and is per process, which is
enough for the single-process service (constitution VI: the simplest design that works).
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Hashable

MAX_ATTEMPTS = 5
WINDOW_S = 600.0


class AttemptLimiter:
    """At most ``max_attempts`` attempts per key within any ``window_s`` seconds."""

    def __init__(
        self,
        max_attempts: int = MAX_ATTEMPTS,
        window_s: float = WINDOW_S,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_attempts = max_attempts
        self.window_s = window_s
        self._monotonic = monotonic
        self._attempts: dict[Hashable, deque[float]] = {}

    def try_attempt(self, key: Hashable) -> float:
        """Record an attempt for ``key`` and return 0.0, or, over the limit, record nothing and
        return the seconds until the next attempt is allowed."""
        now = self._monotonic()
        self._prune(now)
        attempts = self._attempts.setdefault(key, deque())
        if len(attempts) >= self.max_attempts:
            return max(0.0, attempts[0] + self.window_s - now)
        attempts.append(now)
        return 0.0

    def _prune(self, now: float) -> None:
        horizon = now - self.window_s
        for key in list(self._attempts):
            attempts = self._attempts[key]
            while attempts and attempts[0] <= horizon:
                attempts.popleft()
            if not attempts:
                del self._attempts[key]

    def __len__(self) -> int:
        """Keys with an attempt in the window (memory stays bounded by active users)."""
        return len(self._attempts)


__all__ = ["MAX_ATTEMPTS", "WINDOW_S", "AttemptLimiter"]
