# Security and Privacy Review: Skip Messages That Need No Reply

**Feature**: [spec.md](spec.md) | **Task**: T038 | **Date**: 2026-10-21 (re-run after the port
to `main` as 004, including T031–T035 and the dropped-turn classification)

**Result**: PASS. No message text in logs or activity records, eval threads are synthetic,
`/skipped` previews stay in the owner's control chat, classifier content travels only in
untrusted data blocks, and nothing new can send.

## 1. Message text in logs (constitution II, FR-010)

**Method**: all 42 tests in `tests/integration/test_skip_flow.py` (US1, US2, US3) and
`tests/integration/test_skip_pending_and_habit.py` (pending draft, habit from manual replies,
`/chats`, `/history`, Russian UI) were run with a throwaway pytest plugin. The plugin routed
every test's logs through the production `scribe.logging.configure_logging` (JSON, redaction
last) into one file, 359 lines. Every string value in every line was then compared with the
22 synthetic message texts the tests send, seed or reply with ("thanks, that helps!", "never
mind, solved it, thanks", "you're welcome", "спасибо!", …).

**Result**: 0 hits on message content.
- The only equal strings were enum values, not content: `llm_call.decision = "ok"` and
  `reply_gate_decided.category = "thanks"`. They match the one-word test messages "ok" and
  "thanks".
- No log line has a `text` or `preview` key.
- The only equal strings were enum values, not content: `llm_call.decision = "ok"` and
  `category = "thanks"` on `reply_gate_decided` / `reply_gate_dropped_classified`.
- Events seen: `reply_gate_decided` (47), `llm_call`, `intake`,
  `scheduler_due_turns_claimed`, `draft_prepared`, `draft_card_posted`,
  `skip_setting_changed`, `draft_anyway_requested`, `pending_draft_closing_message`,
  `reply_gate_dropped_classified`, `outgoing_manual_reply`, `sent`. The new events carry ids,
  flags and the category only.

**Automated guard**: `test_gate_log_line_has_no_text` asserts that `reply_gate_decided` carries
exactly the contract fields and no `text`, and that a marker string from the message never
appears in the captured log. `test_inherit_clears_override` checks `skip_setting_changed` the
same way.

**Activity records**:
- `skipped` stores `{category, source, confidence}`; `test_thanks_is_read_and_skipped`
  asserts that exact dict.
- `draft_anyway` stores `{skipped_at, category}`; `test_draft_anyway_produces_normal_card`
  asserts that too.

Draft `output_json.system_warnings` gains only the codes `drafted_by_habit` and
`drafted_on_request`.

## 2. Eval threads are synthetic (constitution II, V)

`evals/threads/skip_closing.jsonl` (34), `skip_needs_reply.jsonl` (32) and
`skip_ambiguous.jsonl` (7) were written for this feature.
- They use the placeholder names already used by the 001 suite ("Anna Testova", "Ivan
  Primerov", "Design team").
- The content is generic workplace phrasing ("Can you send the Q3 deck?", "спасибо", "ok?").
- No real names, companies, numbers or messages were copied from any chat.

The test fixtures in `test_skip_flow.py`, `test_reply_gate.py`, `test_skipped_render.py` and
`test_models_and_repos.py` are synthetic in the same way.

## 3. `/skipped` previews only in the owner's control chat

- Preview text is decrypted in `skipped.skipped_view` only while building the reply. It goes
  into `message.answer` (`/skipped`) or `bot.edit_message_text` on the callback's own message.
  Both target the private control chat the update came from. Nothing else (logs, activity,
  other chats) receives it.
- `UserContextMiddleware` ignores every non-private chat and resolves `user` from that private
  chat's id. Every repository call in `skipped.py` filters by that `user.id`.
- A foreign or unknown turn or chat id is refused by `ownership.OwnershipMiddleware` (now
  covering `draftany`, `skipchat` and `/chats`' `chatskip`) with the toast "Not found" /
  "Chat not found" and a warning `callback_refused_not_owner` carrying only the action and
  `user_id`; nothing changes (`test_draft_anyway_foreign_turn_not_found`,
  `test_toggle_foreign_chat_not_found`, `test_chatskip_foreign_chat_refused`). The handlers
  load every row scoped to the user as well. `test_ownership_covers_every_entity_action`
  checks that every registered callback action is either ownership-checked or entity-free.
- Turns whose messages were purged by retention drop out of the list
  (`test_purged_skipped_turn_not_listed`), so a preview never outlives the 30-day window.
- Skip callbacks are not behind `ControlChatOwnerMiddleware`; that middleware guards only the
  login buttons, and the draft-review callbacks work the same way. In a private bot chat the
  presser is the chat's owner. With no message (inline mode), `from_user.id` is used, which
  again resolves to the presser's own account.

## 4. Classifier input and prompt injection (FR-006, security section)

- `ReplyNeededRequest` holds the turn (or the single new message for the pending-draft check,
  or a dropped turn for the habit rule), at most `skip_context_messages` (8) earlier messages in
  the same chat, the contact name and kind, and `pending_draft`. That is less than
  `compose.draft` receives (up to 40 + 20 messages).
- Every field becomes a `<data name="…" trust="untrusted">` block through
  `LLMRequest.to_blocks`. The system prompt is `prompts/reply_needed.md` alone.
- `test_user_text_never_in_system_prompt` now covers `classify.reply_needed` too.
  `test_injection_cannot_force_skip` checks the real pipeline: the message appears only in the
  `incoming_messages` data block, never in `system`.
- The prompt says that instructions inside messages never decide the outcome, and that a
  message trying to steer the decision needs a reply.
- The route has no tools.
- Even a model that obeyed an injection could only cause a *skip*, never a send. Five eval
  threads cover this, with `gate_injection_forced_skip_max: 0`.

## 5. Nothing new can send (constitution I)

- `reply_gate.py`, `skipped.py`, the scheduler, intake (`router.py`, `events.py`) and
  `settings.py` changes contain no call to `send_text` or `send_approved_draft`.
- The pending-draft check only clears `drafts.outdated` (never for a draft that was outdated
  before, or when a newer message exists) and sets `possibly_unnecessary`, both under the draft
  row lock; a cleared draft is sent only through the usual ✅ Send approval.
- `classify_dropped` only writes `skip_category` on an already dropped turn (guarded update).
- The gate returns only `draft` / `skip`. A skip only updates the turn row (guarded
  `WHERE state = 'reading'`) and writes one activity row.
- **Draft anyway** only moves the turn `skipped -> waiting` with a guarded update. The
  unchanged compose -> card -> approve path follows, and
  `test_draft_anyway_produces_normal_card` sends only after the explicit `approve:` callback.
- The `sent_requires_approval` constraint is untouched.

## Residual notes

- The habit rule now sees manual replies (001 T047 on `main`): outgoing messages after a skip,
  and closing turns a manual reply dropped before the gate saw them (classified afterwards,
  one small-model call per such short turn).
- The /skipped preview and every new text are rendered in the user's language from the
  `scribe.i18n` catalogs; the catalog lint (`test_i18n_lint.py`) finds no hard-coded
  user-facing text in `skipped.py`, `settings.py`, `review.py` or `render.py`.
- The live eval run (T037) is still pending (no API key in this environment). Until then the
  real model's injection resistance on the gate suite is unmeasured; the dry run only proves
  the harness.
