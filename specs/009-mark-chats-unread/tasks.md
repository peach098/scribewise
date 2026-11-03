# Tasks: Mark Chats Unread After Agent Activity

**Input**: Design documents from `/specs/009-mark-chats-unread/`

**Prerequisites**: plan.md, spec.md, research.md (U1–U10), data-model.md, contracts/
(internal-interfaces.md, control-chat.md), quickstart.md. Base: `main` at `2e8a82e`.

**Tests**: Included (constitution Principle V: deterministic code is tested before or with the
code). Test file names match `quickstart.md`. No evaluation run: draft generation is unchanged.

**Organization**: US1 (mark after the agent reads) is the MVP. US2 (mark after an approved
send) and US3 (seen signals, no re-mark) build on the foundational `UnreadMarker`. US4 adds the
`/settings` switch. The setting column and the marker's "setting off" check are foundational,
so US1–US3 work with the default (on) before US4 lands.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: US1–US4 (user story phases only)

---

## Phase 1: Setup

- [X] T001 Verify the base: branch `009-mark-chats-unread` rebased on `origin/main`; `uv run pytest -q`, `make lint typecheck` pass; `alembic/versions/0008_skip_closing_messages.py` is the head revision (if another branch merged `0009_*` meanwhile, renumber this feature's migration to the next free number and set `down_revision` accordingly, research U9)

---

## Phase 2: Foundational (blocking for all stories)

### Data

- [X] T002 [P] Migration test `test_migration_0009_mark_unread` in `tests/integration/test_models_and_repos.py`: after upgrade, existing users have `mark_unread = true` and existing work chats `unread_mark_floor IS NULL`; `unread_mark_floor = 0` violates `CHECK (unread_mark_floor IS NULL OR unread_mark_floor > 0)`; downgrade drops both columns
- [X] T003 Add `mark_unread: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=SERVER_TRUE)` to `User` and `unread_mark_floor: Mapped[int | None] = mapped_column(BigInteger)` plus the check constraint `unread_mark_floor_positive` to `WorkChat` in `src/scribe/db/models.py` (comments citing specs/009 FR-009 / FR-005); write `alembic/versions/0009_mark_unread.py` (`revision="0009"`, `down_revision="0008"`; `users.mark_unread BOOLEAN NOT NULL DEFAULT true`, `work_chats.unread_mark_floor BIGINT NULL` with the check; downgrade drops the constraint and both columns) (makes T002 pass)

### Gateway

- [X] T004 [P] In `src/scribe/telegram/gateway.py` add the frozen dataclass `UnreadMarkClearedEvent(user_id: uuid.UUID, chat_id: int)`, `UnreadMarkClearedHandler`, and the protocol methods `mark_unread(user_id, chat_id) -> None` and `on_unread_mark_cleared(handler) -> None` with the docstrings from contracts/internal-interfaces.md; add to the module docstring: "`mark_unread` is called exclusively by `telegram/unread_mark.UnreadMarker`; it never sleeps through a flood wait and never removes a mark"
- [X] T005 [P] Extend `src/scribe/telegram/fake_gateway.py` (depends on T004): `mark_unread_calls: list[tuple[uuid.UUID, int]]`, `fail_mark_unread_with: Exception | None`, `mark_unread_delay_s: float = 0.0` (awaited before recording, to test the timeout), `call_order: list[tuple[str, int]]` appended by `mark_read`, `send_text` and `mark_unread` (`("mark_read", chat_id)` etc.) so tests can assert "mark after read/send", an optional `on_mark_read: Callable[[MarkReadCall], Awaitable[None]] | None` hook awaited inside `mark_read` (to emit an echo `ReadEvent` mid-read), `_unread_cleared_handlers`, `on_unread_mark_cleared`, `emit_unread_mark_cleared(event)`; `reset_calls()` also clears `mark_unread_calls` and `call_order`
- [X] T006 [P] Contract tests `tests/contract/test_gateway_mark_unread.py` against a faked Telethon client (pattern of `tests/unit/test_user_client.py` and `tests/contract/test_gateway_search.py`): `UserClient.mark_unread(chat_id)` resolves the entity and sends exactly one `functions.messages.MarkDialogUnreadRequest` with `peer=types.InputDialogPeer(peer=<entity>)` and `unread=True`; a `FloodWaitError` of any length raises `GatewayBusy(seconds)` **without sleeping** (fake sleep not called); a request type already flood-waited raises `GatewayBusy` before sending; `UnauthorizedError` → `SessionRevoked`; `ClientPool.mark_unread` with no running client → `GatewayNotStarted`; `ClientPool.mark_unread` with a revoked session marks the session revoked like the other operations
- [X] T007 Implement `UserClient.mark_unread` (via `_resolve` and `_no_sleep`) in `src/scribe/telegram/user_client.py`, add `mark_unread` to `UserClientLike` and `ClientPool.mark_unread` (`_call`) in `src/scribe/telegram/client_pool.py`; update the `user_client.py` module docstring (makes T006 pass; depends on T004)

### UnreadMarker core

- [X] T008 [P] Tests `tests/integration/test_unread_mark_marker.py` for the marker alone (real DB, `FakeMessagingGateway`): `prepare` returns a ticket for an enabled work chat with `floor` NULL or `< up_to`, and `None` when the setting is off (`unread_mark_skipped reason=disabled`), the chat is unknown or disabled (`reason=not_work_chat`), or `floor >= up_to` (`reason=seen`); `prepare` writes nothing; `commit(ticket)` sets `floor = up_to` when it is NULL or `<= up_to` and calls `mark_unread` once (log `unread_mark_set cause=read up_to=<n>`), skips with `reason=seen` when `floor > up_to`; `commit(None)` is a no-op; the floor is committed before `mark_unread` is called (assert inside a `mark_unread` fake hook that a fresh session already sees the new floor); `mark_unread` raising `GatewayBusy` / `GatewayNotStarted` / `SessionRevoked` / `RuntimeError` is swallowed and logged `unread_mark_failed error=<type>`; `mark_unread_delay_s = 10` with `timeout_s = 0.2` returns within ~0.2 s and logs `unread_mark_failed error=TimeoutError`; no method ever calls `mark_read` or `send_text`
- [X] T009 Create `src/scribe/telegram/unread_mark.py` with `MarkTicket` and `UnreadMarker(session_factory, gateway, *, timeout_s=5.0)` implementing `prepare` and `commit` per contracts/internal-interfaces.md and data-model.md "Transitions" (conditional `UPDATE work_chats SET unread_mark_floor = :n WHERE id = :id AND (unread_mark_floor IS NULL OR unread_mark_floor <= :n) RETURNING id` in its own transaction, then `asyncio.wait_for(gateway.mark_unread(...), timeout_s)`); every public method catches all exceptions, logs and returns; structured events per data-model.md "Logs" with ids only; module docstring explains research U4 (decide before the agent's read, commit after) (makes T008 pass; depends on T003, T005)

**Checkpoint**: columns, gateway method, fake and marker core in place; whole suite green.

---

## Phase 3: User Story 1 - Chats the agent read stay visible (Priority: P1) 🎯 MVP

**Goal**: right after the agent reads new messages in a work chat, the chat is marked unread
once; nothing is marked for messages the agent does not read; failures never affect reading or
drafting.

**Independent Test**: quickstart §1 read cases and §2 Q1, Q4, Q5.

### Tests for User Story 1

- [X] T010 [P] [US1] Integration tests in `tests/integration/test_unread_mark.py` (synthetic users, chats and messages from `tests/integration/us2_support.py`; `DueTurnsProcessor` with a `UnreadMarker`): a message read after its delay → `call_order` is `mark_read` then `mark_unread` for that chat, exactly once, and `unread_mark_floor` equals the read-up-to id; a closing message the reply gate skips (004) is still marked; two turns of the same chat due in one tick → the floor ends at the newest id and at most one `mark_unread` per distinct read-up-to id; a media-only turn (`handle_manually`), a paused user and an access-suspended user → no `mark_unread`; `mark_read` failing → no `mark_unread`, floor unchanged, and the retried read marks once; `fail_mark_unread_with = GatewayBusy(30)` → the turn still reaches `pending_review` with a card and no control-chat notice; `requeue_interrupted_turns` + re-read of an already-marked turn → no second `mark_unread`; a processor built without a marker behaves exactly as today (no `mark_unread_calls`)
- [X] T011 [P] [US1] Integration test in `tests/integration/test_unread_mark.py` for "Regenerate" of an outdated draft (`review.py` `_mark_turn_read`): when a newer message joined the turn → one `mark_unread` after the read; when the same draft is regenerated again with nothing new → none

### Implementation for User Story 1

- [X] T012 [US1] In `src/scribe/scheduling/scheduler.py` give `DueTurnsProcessor.__init__` an optional `marker: UnreadMarker | None = None`; in `_read`, after the `< 1` early return and before `gateway.mark_read`, `ticket = await marker.prepare(target.user_id, target.telegram_chat_id, target.telegram_message_id)`, and after a successful `mark_read` `await marker.commit(ticket)` (not on failure, not on the early returns); document it in the module docstring ("specs/009: the chat is marked unread right after the read") (makes T010 pass; depends on T009)
- [X] T013 [US1] In `src/scribe/telegram/control_bot/review.py` `_mark_turn_read`: take an optional `marker` (from the handler's workflow data key `unread_marker`, default `None`) and wrap its `gateway.mark_read` with `prepare` / `commit` the same way; thread the parameter from the Regenerate callback (makes T011 pass; depends on T009)
- [X] T014 [US1] Wire in `src/scribe/__main__.py`: build one `UnreadMarker(self.session_factory, self.pool)` before the dispatcher, pass `unread_marker=` to `create_dispatcher(...)` and `marker=` to `DueTurnsProcessor(...)` (depends on T012, T013)

**Checkpoint**: MVP: agent reads mark chats unread; quickstart Q1, Q4, Q5 pass manually.

---

## Phase 4: User Story 2 - Chats the agent answered stay visible (Priority: P1)

**Goal**: right after an approved reply is sent, the chat is marked unread; a failed mark never
affects the send.

**Independent Test**: quickstart §1 send cases and §2 Q2.

### Tests for User Story 2

- [X] T015 [P] [US2] Integration tests in `tests/integration/test_unread_mark.py` through the approve and edit-and-send callbacks (`tests/integration/us1_support.py` harness): after `send_approved_draft` succeeds → `call_order` ends with `send_text` then `mark_unread` for that chat and `unread_mark_floor` equals the sent message id; `fail_send_with` set → no `mark_unread`; `fail_mark_unread_with = GatewayBusy(30)` → the draft is `sent`, the card shows "sent", no extra control-chat message, `unread_mark_failed cause=send` logged; the echoed `OutgoingEvent` of the agent's own send is not a manual reply and does not trigger another mark; `send_approved_draft` called without a marker behaves as today; `mark_unread` is not awaited while the draft row lock is held (the fake's hook asserts a concurrent `SELECT ... FOR UPDATE NOWAIT` on the draft succeeds)

### Implementation for User Story 2

- [X] T016 [US2] Add `UnreadMarker.after_send(user_id, work_chat_id, sent_message_id)` to `src/scribe/telegram/unread_mark.py`: setting check, conditional update `unread_mark_floor IS NULL OR unread_mark_floor < :s`, then the bounded mark (`cause=send`), `reason=not_newer` when not updated; extend `tests/integration/test_unread_mark_marker.py` for it (depends on T009)
- [X] T017 [US2] In `src/scribe/sending/send.py` add the keyword `marker: UnreadMarker | None = None` to `send_approved_draft` and, after `session.commit()` and before commitment extraction, `await marker.after_send(user_id, work_chat_id, telegram_message_id)`; update the module docstring; pass `marker=unread_marker` at every `send_approved_draft` call site in `src/scribe/telegram/control_bot/review.py` (`grep -rn send_approved_draft src`) (makes T015 pass; depends on T014, T016)

**Checkpoint**: reads and sends both mark; quickstart Q2 passes manually.

---

## Phase 5: User Story 3 - The mark goes away when the user looks, and stays away (Priority: P1)

**Goal**: reads, mark clearing and manual messages from the user's own sessions move the floor,
so the agent never marks what the user has seen and never re-marks without new activity.

**Independent Test**: quickstart §1 seen / loop cases and §2 Q3, Q6, Q7, Q8.

### Tests for User Story 3

- [X] T018 [P] [US3] Unit tests `tests/unit/test_unread_mark_updates.py` for `unread_mark_cleared_from_update(user_id, update)`: `UpdateDialogUnreadMark(peer=DialogPeer(PeerUser(5)), unread=False)` → event with chat id `5`; `PeerChat` / `PeerChannel` → marked ids via `utils.get_peer_id`; `unread=True` → `None`; `unread=None` → `None`; `DialogPeerFolder` → `None`; a set `saved_peer_id` → `None`; any other update type → `None`
- [X] T019 [P] [US3] Contract tests in `tests/contract/test_gateway_mark_unread.py`: after `UserClient.start`, the raw handler is registered for `UpdateDialogUnreadMark` as well as the two read updates; feeding it an `unread=False` update awaits `on_unread_mark_cleared` with the mapped event, an `unread=True` update awaits nothing; a handler exception is logged (`telegram_read_handler_failed`) and swallowed; `ClientPool.on_unread_mark_cleared` handlers receive events from every user's client (fan-out, one failing handler does not stop the next)
- [X] T020 [P] [US3] Integration tests in `tests/integration/test_unread_mark.py`: the user reads the chat elsewhere (`emit_read_elsewhere`, max id ≥ the waiting message) before the delay → the turn becomes due (existing behavior) and is read, but **no** `mark_unread` (`reason=seen`); after a mark, `emit_unread_mark_cleared` → floor = newest stored message id of the chat (`unread_mark_seen source=mark_cleared`), no `mark_read` and no `mark_unread` follow (FR-008), and a re-read of the same turn marks nothing; the next new incoming message read by the agent → exactly one new mark; a manual outgoing message (`emit_outgoing`, not an agent send) → floor = its id (`source=manual_reply`); an echo `ReadEvent` of the agent's own read emitted from the fake's `on_mark_read` hook (between `mark_read` and `commit`) → the chat **is** still marked (research U4); with `users.mark_unread = false`, seen signals still move the floor, and after switching it on an agent re-read of an already-seen message marks nothing; seen signals for a non-work or disabled chat change nothing

### Implementation for User Story 3

- [X] T021 [US3] In `src/scribe/telegram/user_client.py` add the pure `unread_mark_cleared_from_update` next to `read_event_from_update`, the constructor argument `on_unread_mark_cleared: UnreadMarkClearedHandler`, `types.UpdateDialogUnreadMark` in the `events.Raw` types in `start()`, and dispatch in `_handle_raw`; in `src/scribe/telegram/client_pool.py` add `_unread_cleared_handlers`, `on_unread_mark_cleared`, `_dispatch_unread_cleared`, and pass `on_unread_mark_cleared=` in `_new_client`; update both module docstrings (makes T018, T019 pass; depends on T004)
- [X] T022 [US3] Add `UnreadMarker.seen(user_id, telegram_chat_id, up_to, *, source)` (`up_to=None` → `SELECT max(telegram_message_id) FROM messages WHERE user_id = … AND work_chat_id = …`; `UPDATE work_chats SET unread_mark_floor = GREATEST(COALESCE(unread_mark_floor, 0), :m)` for an enabled work chat only; nothing when there is no stored message; log `unread_mark_seen`) and `handle_unread_mark_cleared(event)` to `src/scribe/telegram/unread_mark.py`; extend `tests/integration/test_unread_mark_marker.py` (depends on T009)
- [X] T023 [US3] In `src/scribe/intake/events.py` give `ChatEventHandler` an optional `marker: UnreadMarker | None = None`; in `handle_read_elsewhere`, after the commit and for an enabled work chat, `await marker.seen(user_id, event.chat_id, event.max_message_id, source="read_elsewhere")`; in `handle_outgoing`, after the lock is released and only for a manual message (`not outcome.agent_send`), `await marker.seen(user_id, event.chat_id, event.message_id, source="manual_reply")`; extend the module docstring (depends on T022)
- [X] T024 [US3] Wire in `src/scribe/__main__.py`: pass `marker=` to `ChatEventHandler(...)` and register `self.pool.on_unread_mark_cleared(marker.handle_unread_mark_cleared)` (makes T020 pass; depends on T014, T021, T023)

**Checkpoint**: no re-marking after the user looked; quickstart Q3, Q6, Q7, Q8 pass manually.

---

## Phase 6: User Story 4 - The user can turn it off (Priority: P2)

**Goal**: `/settings` shows "🔵 Mark chats unread after the agent: on/off" with a toggle;
off stops all marking at once; on never backfills.

**Independent Test**: quickstart §1 setting cases and §2 Q9.

### Tests for User Story 4

- [X] T025 [P] [US4] Tests `tests/integration/test_settings_mark_unread_toggle.py` (pattern of `tests/integration/test_settings_search_toggle.py`): `format_callback("settings", "mark_unread") == "settings:mark_unread"`; a press flips `users.mark_unread` and edits the `/settings` message in place (text line and button label change; the search-earlier line and button are unchanged); a second press flips it back; a press without an editable message sends a fresh `/settings`; Russian texts per contracts/control-chat.md; a new user has the setting on; with the setting off, an agent read and an approved send produce no `mark_unread` (`reason=disabled`); after turning it on, no chat is marked until the next read or send; `/my_data` export (`retention/user_data.export_user_data`) contains `mark_unread` in the `users` row and `unread_mark_floor` in `work_chats` rows; `/delete_my_data` leaves no row with either

### Implementation for User Story 4

- [X] T026 [P] [US4] Add the keys from contracts/control-chat.md to `src/scribe/i18n/en.py` and `src/scribe/i18n/ru.py` (`settings.mark_unread`, `settings.mark_unread_state.on|off`, `btn.mark_unread_on|off`) next to the `search_earlier` keys; `tests/unit/test_i18n_lint.py` and `tests/unit/test_i18n.py` must stay green
- [X] T027 [US4] In `src/scribe/telegram/control_bot/settings.py`: `MARK_UNREAD = "mark_unread"`; a `settings.mark_unread` line after the search-earlier line in `settings_text`; `settings_keyboard(lang, search_earlier=True, mark_unread=True)` with one new row after the search-earlier button; `_settings_keyboard_of` passes `user.mark_unread`; `_toggle_mark_unread` mirroring `_toggle_search_earlier` (flip, save via `_save_user`, re-render in place); dispatch in `settings_menu`; module docstring lists `settings:mark_unread` (specs/009) (makes T025 pass; depends on T003, T026)

**Checkpoint**: all four stories work independently; quickstart Q9 passes manually.

---

## Phase 7: Polish & Cross-Cutting

- [X] T028 [P] Extend `tests/unit/test_logging_redaction.py` (or the closest existing redaction test): `unread_mark_set`, `unread_mark_skipped`, `unread_mark_failed` and `unread_mark_seen` events carry only ids, `cause`, `reason`, `source`, `up_to` and `error` type names, never message text or chat titles (FR-012, Principle II)
- [X] T029 [P] Docs: add a bullet on unread marks (what is marked, when it clears, the `/settings` switch) to the agent-behavior list in `README.md` and `README.ru.md`, and add `specs/009-mark-chats-unread/` to the list of specs at the end of both READMEs
- [ ] T030 Run `uv run pytest -q`, `make lint typecheck` and the quickstart §2 manual run Q1–Q9 on a test account; record the outcome of Q1–Q4 (whether reading and sending cleared the mark server-side, whether the agent's own read was echoed back; research U2, U4) under a "Manual run" heading at the end of `specs/009-mark-chats-unread/quickstart.md`

---

## Dependencies & Execution Order

- **Setup (T001)** → **Foundational (T002–T009)** → stories.
- Inside Foundational: T002 → T003; T004 → T005, T007; T006 ∥ T004 (test first) → T007;
  T008 ∥ T003–T005 → T009 (needs T003, T005).
- **US1 (T010–T014)** needs T009. **US2 (T015–T017)** needs T009 and T014 (wiring of the
  marker into the dispatcher). **US3 (T018–T024)** needs T009; T024 needs T014.
  **US4 (T025–T027)** needs only T003 for the UI; its "setting off → no marks" assertions need
  US1 and US2 code.
- **Polish (T028–T030)** after the stories it covers.
- Story order for one developer: US1 → US2 → US3 → US4. US3 and US4 can run in parallel with
  US2 once T014 is done.

## Parallel Opportunities

- Foundational: T002, T004, T006, T008 (four different test/source files) together; then T005
  and T007 together.
- US1: T010 and T011 (same file, different test functions; write in one sitting or split the
  file) in parallel with US3's T018 and T019.
- US4: T025 and T026 together, before T027.
- Polish: T028 and T029 together.

### Parallel example: Foundational

```text
Task: "T002 Migration test test_migration_0009_mark_unread in tests/integration/test_models_and_repos.py"
Task: "T004 UnreadMarkClearedEvent + protocol methods in src/scribe/telegram/gateway.py"
Task: "T006 Contract tests tests/contract/test_gateway_mark_unread.py"
Task: "T008 Marker tests tests/integration/test_unread_mark_marker.py"
```

### Parallel example: after T014

```text
Task: "T015-T017 US2 send path (unread_mark.after_send, send.py, review.py)"
Task: "T018-T019 US3 update mapping and raw handler tests"
Task: "T025-T026 US4 settings tests and i18n keys"
```

## Implementation Strategy

1. **MVP (US1)**: T001–T014. Chats the agent reads are marked unread once; this alone answers
   the issue's first acceptance criterion and is safe to ship (default on, failures invisible).
2. **US2**: re-mark after sending (second half of the first acceptance criterion).
3. **US3**: seen signals. This makes the second acceptance criterion hold even when the user
   reads before the agent does. Without it, the MVP still never re-marks a message it already
   marked (floor), but it would mark messages the user read during the delay.
4. **US4**: the `/settings` switch (third acceptance criterion).
5. **Polish**: redaction test, README, manual run.

**Task count**: 30 (Setup 1, Foundational 8, US1 5, US2 3, US3 7, US4 3, Polish 3).
