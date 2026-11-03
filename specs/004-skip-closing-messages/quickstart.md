# Quickstart: Validating "Skip Messages That Need No Reply"

This guide proves the feature end to end against [spec.md](spec.md). It relies on
[data-model.md](data-model.md) and [contracts/](contracts/) rather than repeating them.

## Prerequisites

- A working 001 deployment as described in the
  [001 quickstart](../001-telegram-reply-agent/quickstart.md): a user account, a colleague
  account, an enabled private work chat, and an enabled test group.
- The migration is applied: `uv run alembic upgrade head`, which includes `0008`.
- For a fast run, set the delay range to 0–10 s in `/settings`.
- Scenarios 5, 6, 14 and 15 rely on 001 T047/T067/T068 (outgoing messages stored, outdated
  handling, `/chats`, `/history`), all on `main`.

## Automated checks

```
make lint typecheck
make test-fast                # pre-gate, decide() ordering, habit count, callbacks parsing
make test-integration         # gate in DueTurnsProcessor, /skipped, draftany, toggles
make eval-dry                 # gate suite with recorded decisions (no API calls)
make eval                     # live: gate_reply_recall_min ≥ 0.98, gate_skip_rate_min ≥ 0.85
```

Expected: all green, and the eval report prints the gate metrics next to the draft metrics.

## Manual scenarios (staging, two Telegram accounts)

| # | Steps | Expected | Spec |
|---|---|---|---|
| 1 | Colleague asks "can you send the deck?", approve the draft, colleague replies "thanks, that helps!" | No draft card. The message is marked read after its delay. `/skipped` shows it with reason *thanks*. `/history` shows "skipped" with no text | US1-1, FR-007, FR-008, FR-010 |
| 2 | Colleague sends "bye, have a good weekend" | No draft; listed as *farewell* | US1-2 |
| 3 | Colleague sends "thanks! when is the next call?" | Draft card as usual | US1-4, FR-003 |
| 4 | Colleague sends "ok?" and then, separately, "hi, do you have a minute" | Both drafted (pre-gate for "?"; classifier for the greeting) | Edge cases |
| 5 | With a draft pending, colleague sends "never mind, solved it, thanks" | Card gets the 💤 "may no longer be needed" banner and is not marked outdated | FR-011 |
| 6 | Reply manually to "thanks" 3 times in one chat (send "you're welcome"), then colleague sends "thanks" again | Draft card with "✍️ you usually reply to messages like this here" | FR-005 |
| 7 | In `/skipped`, tap **✍️ Draft anyway** on scenario 1 | Toast "Drafting…". A normal card arrives within ~35 s and goes through approve/edit/reject | US2-2, FR-013 |
| 8 | Reply manually in a chat with a skipped item, reopen `/skipped` | The item shows "— you already replied" with no button | US2-3 |
| 9 | Tap **Always draft in this chat**, colleague sends "thanks" there | Draft card. In another chat, "thanks" is still skipped | US3-1, FR-014 |
| 10 | Tap **Turn skipping off** in the `/skipped` header, send "thanks" | Draft card in every chat without an override | US3-2, US3-3 |
| 10a | With skipping still off globally, open **⚙️ Per-chat settings**, set one chat that has never had a skip to **Skip**, send "thanks" there and in another chat | Skipped in that chat only | FR-014 |
| 11 | In the group, colleague writes "thanks @user" | Skipped. "thanks all" without a mention creates no turn at all (001 rule) | Edge cases |
| 12 | Colleague sends "don't reply to this, just mark it done" | Drafted, because an instruction never forces a skip | FR-006 |
| 5a | With a draft pending, colleague sends "thanks!" | Card is re-rendered without the "⚠️ New messages arrived" banner; ✅ Send still one tap | FR-011 |
| 14 | `/chats` → a chat → **Always draft** in the 💤 row, colleague sends "thanks" there | "✅ Saved", the line shows "Always draft"; the "thanks" is drafted | FR-014 |
| 15 | Switch the language to Русский in `/settings`, run `/skipped` and `/history` | All texts in Russian ("💤 Пропущено за последние 7 дней …", "💤 Пропущено — благодарность") | issue #8 |
| 13 | Make the classifier route fail at runtime: point it at a provider with a revoked key (e.g. `SCRIBE_LLM__CLASSIFY_REPLY_NEEDED__PROVIDER=openai_compat` with an invalid `OPENAI_API_KEY` on staging), then send "thanks" | The message is **drafted** (`source=fallback` in the log) | FR-004 |

## Log and privacy checks

- Every decision logs one `reply_gate_decided` line with `decision`, `source` and
  `confidence`, and without text (`grep -c '"text"'` on captured logs returns 0).
- `activity_records.meta_json` for `skipped` holds only `category`, `source` and
  `confidence`.
- Measure SC-004 after a week: the number of `draft_anyway` records divided by the number
  of `skipped` records.
