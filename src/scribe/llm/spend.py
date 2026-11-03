"""Per-user LLM spend (issue #67): what the router hands to a :class:`SpendSink` after each call.

The router knows the route, the user, the usage and the cost; storage (the daily ``llm_spend``
aggregate, ``scribe.db.repos.llm_spend``) is plugged in by the service, so ``scribe.llm`` stays
free of the database. A spend entry carries counts and money only, never content.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from scribe.llm.provider import RouteName, Usage


@dataclass(frozen=True, slots=True)
class SpendEntry:
    user_id: uuid.UUID
    route: RouteName
    at: datetime  # when the call started (UTC); its UTC date is the aggregate's day
    usage: Usage
    cost_usd: float
    failed: bool = False  # a failed call the provider still billed (truncated, invalid output)


class SpendSink(Protocol):
    async def __call__(self, entry: SpendEntry) -> None: ...
