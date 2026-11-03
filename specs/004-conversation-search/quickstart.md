# Quickstart: Search Over Earlier Conversations

**Feature**: [spec.md](spec.md) | Interfaces: [contracts/internal-interfaces.md](contracts/internal-interfaces.md)

How to prove the feature works. Implementation details live in `tasks.md`.

## Prerequisites

- Dev environment as in the README (`uv sync`, Postgres via `docker compose up -d db`).
- `uv run alembic upgrade head` (applies `0007_search_earlier`).

## 1. Automated checks

```bash
uv run pytest -q tests/unit/test_earlier_link_rules.py tests/unit/test_llm_router.py
uv run pytest -q tests/contract/test_gateway_search.py
uv run pytest -q tests/integration/test_earlier_search_drafts.py
uv run pytest -q            # whole suite, no regressions
uv run ruff check src tests && uv run mypy src
```

Expected:

- link rules: own / sender / reply / replied-by / mention / thread kept; unrelated group
  messages dropped (FR-S05b);
- gateway: only enabled work chats are searched; ≤ 20 requests per draft; a flood wait raises
  `GatewayBusy` before sending, the rest is cut by the 6 s deadline (FR-S01, FR-S18);
- drafts: an agreement 60 messages back is cited as `earlier:*` and listed on the card;
  failure → `earlier_search_unavailable`; no match → `earlier_search_no_match`; setting off →
  `earlier_search_off`; "ok, thanks" → no search call at all (FR-S05);
- logs contain no query or message text (FR-S17).

## 2. Evaluation (Principle V)

```bash
uv run python evals/run.py --suite earlier_context
uv run python evals/run.py --suite drafts   # filter prompt v2: no regression
uv run python evals/run.py --suite jira
```

Thresholds in `evals/thresholds.yml` (`earlier_context`): decision recall ≥ 0.9, precision
≥ 0.8; correct earlier citation ≥ 0.8 (SC-001); invented referenced facts = 0 (SC-002);
injection threads never widen scope.

## 3. Manual run on a test account

1. Connect a test Telegram account; enable one private chat as a work chat.
2. From the contact, send "The price for the pilot is 120 000, agreed?" and reply from the
   user "Agreed". Then send 60 unrelated messages (or use an older chat with history).
3. From the contact: "Is the price still as we agreed?"
4. Expected: the draft states 120 000; the card shows `💬 <chat> · <date>` among sources;
   the Sources view shows the excerpt.
5. `/settings` → turn search off → 🔁 Regenerate: the card shows
   `🔎 earlier conversations not searched`, the draft uses a placeholder for the price.
6. Logs: one `earlier_search` event per draft with `decision`, `outcome`, counts only.

## 4. Group scoping check

In an enabled group: a third participant writes "Anton, the budget is approved at 500k"
replying to the contact; someone else writes an unrelated "budget for the party is 5k". The
contact later asks in private "what budget did we get approved?". Expected: the draft cites the
reply to the contact; the unrelated message is never cited and is counted in
`dropped_unlinked`.
