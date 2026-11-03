# Quickstart: Mark Chats Unread After Agent Activity

**Feature**: [spec.md](spec.md) | Interfaces: [contracts/internal-interfaces.md](contracts/internal-interfaces.md),
[contracts/control-chat.md](contracts/control-chat.md) | Data: [data-model.md](data-model.md)

How to prove the feature works. Implementation details live in `tasks.md`.

## Prerequisites

- Dev environment as in the README (`uv sync`, Postgres via `docker compose up -d db`).
- `uv run alembic upgrade head` (applies `0009_mark_unread`).

## 1. Automated checks

```bash
uv run pytest -q tests/unit/test_unread_mark_updates.py
uv run pytest -q tests/contract/test_gateway_mark_unread.py
uv run pytest -q tests/integration/test_unread_mark_marker.py tests/integration/test_unread_mark.py \
  tests/integration/test_settings_mark_unread_toggle.py
uv run pytest -q            # whole suite, no regressions
make lint typecheck
```

Expected:

- an agent read of a new message → exactly one `mark_unread` call for that chat, after the
  `mark_read` call (US1);
- an approved send → one `mark_unread` after `send_text` (US2);
- read elsewhere / mark cleared / manual reply before the agent's read → no mark (US3-2, US3-3);
- retry, requeue after restart, Regenerate with nothing new → no second mark (US3-4);
- an echoed `ReadEvent` of the agent's own read arriving between read and commit → still
  marked (US3-5);
- setting off → no `mark_unread` at all; on again → no backfill (US4);
- `mark_unread` raising `GatewayBusy` or hanging past 5 s → the turn is still drafted, the reply
  still counts as sent, `unread_mark_failed` logged, no notice (FR-004, SC-004);
- `UpdateDialogUnreadMark(unread=True)`, a folder peer or a `saved_peer_id` → no event;
- logs contain ids and reasons only, never text (FR-012).

No evaluation run is needed: draft generation is unchanged (constitution, Principle V).

## 2. Manual run on a test account

Use two Telegram accounts: **U** (connected to Scribe) and **C** (the colleague). Enable the
private chat U↔C as a work chat with a delay of 0–1 minutes. Keep U's phone app open on the
chat list.

| # | Step | Expected |
|---|---|---|
| Q1 | C sends "hi, can you check the report?" | After the delay C sees read ticks; in U's chat list the chat has the unread dot; a draft card arrives in the control bot |
| Q2 | Approve the draft | The reply appears in the chat; the chat still (again) has the unread dot |
| Q3 | Open the chat on U's phone, go back, wait 10 minutes | The dot is gone and does not come back |
| Q4 | Restart Scribe, wait 2 minutes | Still no dot |
| Q5 | C sends "thanks" (a closing message, 004) | After the delay: read ticks, the dot is back, no draft card |
| Q6 | C sends a message; before the delay passes U opens the chat on the phone | The message is read by U; when the agent processes it, no dot appears |
| Q7 | C sends a message; U replies by hand from the phone before the delay | The waiting turn is dropped (001); no dot |
| Q8 | U marks a quiet chat unread by hand | The dot stays; the agent never removes it |
| Q9 | `/settings` → "🔵 Unread marks: turn off"; C sends a message | Read ticks after the delay, no dot. Turn on again: no dot until the next message is read |

Logs (`docker compose logs app | grep unread_mark`): one `unread_mark_set` per Q1, Q2, Q5;
`unread_mark_seen` for Q3, Q6, Q7; `unread_mark_skipped` with `reason=seen` for Q6 and
`reason=disabled` for Q9. No message text anywhere.
