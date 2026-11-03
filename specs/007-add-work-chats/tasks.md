---

description: "Task list for Add Work Chats After Setup"
---

# Tasks: Add Work Chats After Setup

**Input**: Design documents from `/specs/007-add-work-chats/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/control-chat.md,
quickstart.md

**Tests**: Included. The issue #32 acceptance criteria require unit and integration tests with
the fake gateway, and Constitution Principle V requires tests for deterministic code.

- **Integration tests** follow `tests/integration/test_control_bot.py` and
  `tests/integration/test_us1_setup_flow.py`: `offline_bot()`, `RecordingSession`, and the
  `text_update` / `callback_update` builders from `tests/integration/bot_support.py`, with
  `FakeMessagingGateway` and the DB fixtures (`session_factory`, `make_user`) in
  `tests/integration`.
- **Synthetic data only**: chat ids such as `-100900000001`, and titles such as
  "Project X (synthetic)".

**Organization**: Tasks are grouped by user story so each story can be implemented and
tested on its own.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1–US4 from spec.md)

## Path Conventions

Single project: `src/scribe/`, `tests/`, `alembic/versions/` at the repository root.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: test seams and texts that every story uses

- [X] T001 [P] Add `fail_list_dialogs: Exception | None = None` to `FakeMessagingGateway`. `list_dialogs` raises it when set. File: src/scribe/telegram/fake_gateway.py.
- [X] T002 [P] Add the new i18n keys in both languages: `btn.chat_add`, `chats.add_prompt`, `chats.add_none_left`, `chats.added` (plural one/other), `chats.add_nothing`, `chats.add_cancelled`, `chats.add_hint`, `chats.add_nothing_to_learn`, `chats.add_learn_failed`, `alert.picker_closed`, `history.kind.chats_added` and `history.more`. Use the texts in contracts/control-chat.md. Files: src/scribe/i18n/en.py and src/scribe/i18n/ru.py. `tests/unit/test_i18n_lint.py` must pass.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: the enum value, the insert-only repo function, the shared picker and the
extracted rebuild. Every story builds on them.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [X] T003 Add `ActivityKind.chats_added = "chats_added"` with the comment `# 007: meta {work_chat_ids}`. File: src/scribe/db/models.py.
- [X] T004 Create migration `0012_chats_added` (revises `0008`). It runs `ALTER TYPE activity_kind ADD VALUE IF NOT EXISTS 'chats_added'` inside `op.get_context().autocommit_block()`, and the downgrade does nothing (see research R8). File: alembic/versions/0012_chats_added.py. Revision 0012 is reserved for this feature (parallel branches use 0009–0011 and 0013, all revising 0008); re-chain `down_revision` at merge.
- [X] T005 [P] Write integration tests for `chats_repo.add_work_chats`. They check that:
  - it inserts new rows with `enabled = true` and the model defaults;
  - it returns only the rows it created;
  - a second call with the same chats returns `[]`;
  - an existing **disabled** row stays disabled and its title is unchanged;
  - other users' rows are never touched.

  File: tests/integration/test_models_and_repos.py, or a new tests/integration/test_add_work_chats_repo.py.
- [X] T006 Implement `NewWorkChat` (a dataclass with `telegram_chat_id`, `title` and `kind`) and `add_work_chats(session, user_id, chats) -> list[WorkChat]`. It uses `INSERT … ON CONFLICT (user_id, telegram_chat_id) DO NOTHING RETURNING` and must "never update existing rows" (data-model.md). File: src/scribe/db/repos/chats.py.
- [X] T007 [P] Write unit tests for the shared picker. They cover:
  - `picker_keyboard` rows, 40-character truncation, ◀️ / ▶️ paging, page clamping and the `Done (N selected)` label, unchanged from today;
  - `available_dialogs(dialogs, existing_ids)`, which drops every existing work chat id, enabled or disabled, and keeps the order.

  File: tests/unit/test_chat_picker.py.
- [X] T008 Create the shared picker module. File: src/scribe/telegram/control_bot/chat_picker.py.
  - Move `picker_keyboard`, `PAGE_SIZE`, `TITLE_CHARS` and `_KIND` there from setup_flow.py, and add `available_dialogs`.
  - Add `show_chat_picker(bot, chat_id, user, state, gateway, *, purpose, picking_state, exclude=frozenset())`. It stores the FSM data `{purpose, dialogs, selected, page, message_id}` described in data-model.md.
  - Add the generic `pick_toggle`, `pick_page` and `pick_done`. `pick_done` dispatches to a finisher by `purpose` through a registry `FINISHERS: dict[str, Callable]`.
  - A callback whose `callback.message.message_id` differs from the stored `message_id` answers `alert.picker_closed` and changes nothing (research R6).
- [X] T009 Refactor the setup flow onto `chat_picker`, with no change in behaviour. File: src/scribe/telegram/control_bot/setup_flow.py.
  - `show_chat_picker` becomes a thin wrapper with `purpose="setup"`, and connect_flow.py keeps calling it.
  - The setup Done logic moves into `finish_pick`, the `setup` finisher. It still answers `setup.pick_none` on an empty selection, upserts, shows `setup.chats_saved` and moves to the time zone step.
  - `create_router` registers the shared handlers under `StateFilter(SetupStates.pick_chats)`.
  - Then register a catch-all for `pickchat:` / `pickpage:` / `pickdone:`, **last** and with no state filter, that answers `alert.picker_closed`.
  - Run tests/integration/test_us1_setup_flow.py and tests/integration/test_connect_flow.py. They must pass unchanged.
- [X] T010 Extract `send_chats_list(bot, chat_id, user, session_factory, lang)` from `chats_command`. It builds the same rows and is reused after Done. `chats_command` calls it. File: src/scribe/telegram/control_bot/settings.py.
- [X] T011 Extract the body of `rebuild_callback` into `rebuild_profiles(bot, user, session_factory, settings, llm_router, gateway, lang, clock, *, wait: bool, only_if_new_in: set[int] | None = None, announce: bool = True, failure_key: str = "profile.rebuild_failed") -> None`. File: src/scribe/telegram/control_bot/profile_command.py. It works as follows:
  - **Lock**: with `wait=False`, a held `profile_build_lock` answers `profile.rebuild_busy`, as today. With `wait=True`, it awaits the lock.
  - **Announce**: `announce=True` sends `profile.rebuilding`.
  - **Build**: it runs `import_own_messages` and then `build_profiles_from_import(interactive=True)`, followed by the existing result, weak and failure texts. When the user was deleted meanwhile, it sends no message.
  - **Nothing new**: when `only_if_new_in` is set and the import has 0 own messages in those chat ids, it builds nothing and sends `chats.add_nothing_to_learn`.
  - **Failure text**: it takes a failure text key, so the add flow can use `chats.add_learn_failed`.

  `rebuild_callback` keeps its confirm step and calls `rebuild_profiles(wait=False)`. The existing tests in tests/integration/test_us4_profiles.py must pass. If PR #31 has merged, keep its `rebuild_result_text` and the `global_profile is None` handling (research R10).

**Checkpoint**: setup works as before on the shared picker. The repo, the enum and the
rebuild helper are ready.

---

## Phase 3: User Story 1 - Add a new work chat from /chats (Priority: P1) 🎯 MVP

**Goal**: ➕ in `/chats` opens the picker with only the new chats. Done adds them as enabled
work chats, confirms "Added N" and shows the updated list. New messages there are drafted.

**Independent Test**: set up a user with 5 fake dialogs, 2 of them work chats (1 disabled).
Check that:
- ➕ lists 3 dialogs;
- picking 1 and tapping Done adds one enabled row, shows "Added 1 chat" and then `/chats`;
- a new incoming message in that chat creates a waiting turn.

### Tests for User Story 1

- [X] T012 [P] [US1] Write the integration tests for adding. They check:
  - ➕ is shown under the list and with `chats.none`, and is absent for a `setting_up` user;
  - a `chatadd:0` tap from a `setting_up` user answers `common.not_set_up`;
  - the picker excludes the enabled and the disabled work chat;
  - Done inserts the selected chats with `enabled = true`, edits the picker message to `chats.added`, and is followed by the `/chats` list;
  - Done with nothing selected gives `chats.add_nothing`, with no rows and no state;
  - a second Done, or a stale snapshot that includes a now-existing disabled chat, creates no duplicate and does not re-enable it.

  File: tests/integration/test_add_chats_flow.py.
- [X] T013 [P] [US1] Write the integration test for monitoring. After Done, `FakeMessagingGateway.emit_new_message` in the added private chat creates a waiting `ConversationTurn`, and a message that arrived before the add (it was never emitted after the add) produces none. In an added group chat, a message without a tag or a reply opens no turn (spec 006 rule; assert the current `mentions_only` default). File: tests/integration/test_add_chats_flow.py.

### Implementation for User Story 1

- [X] T014 [US1] Create the add flow module. File: src/scribe/telegram/control_bot/add_chats.py.
  - Define `AddChatsStates.picking`.
  - Add the `add_chats_callback` handler for `chatadd:`. It returns `common.not_set_up` when `user.state == setting_up`. It loads the existing work chat ids, calls `gateway.list_dialogs`, and passes them to `chat_picker.available_dialogs`. With none left it answers `chats.add_none_left`. Otherwise it calls `show_chat_picker(purpose="add", picking_state=AddChatsStates.picking)` with the `chats.add_prompt` text.
  - Add the `finish_add` finisher, registered as `FINISHERS["add"]`. It clears the state first, then calls `chats_repo.add_work_chats` for the selected dialogs (kind via `_KIND`) and commits. It edits the picker message to `chats.added` (n = number created) or to `chats.add_nothing`, then calls `settings.send_chats_list`. It logs `chats_added` with counts only.
  - `create_router(idle)` registers the shared picker handlers under `StateFilter(AddChatsStates.picking)` and `chatadd:` under `idle`.
- [X] T015 [US1] Wire the add flow into the settings router. File: src/scribe/telegram/control_bot/settings.py.
  - `send_chats_list` appends the `[➕ Add work chats] chatadd:0` row (`btn.chat_add`) when `user.state != setting_up`, also on an empty list.
  - Add `add_chats.AddChatsStates` to the `idle` StateFilter.
  - Include `add_chats.create_router(idle)` in `create_router`, before the setup router sees `pick*`. The settings router is part of the base router, which comes first.
- [X] T016 [US1] Confirm that `chatadd` is **not** in `ENTITY_ACTIONS`, so `OwnershipMiddleware` passes it through, and that the picker callbacks act only on the caller's FSM data. Add a short comment in the module docstring. File: src/scribe/telegram/control_bot/ownership.py (docstring only).

**Checkpoint**: US1 is fully usable. A chat is added and drafted without redoing setup, with
no profile update yet.

---

## Phase 4: User Story 2 - New chats count for my style (Priority: P1)

**Goal**: after a Done that added chats, the Rebuild pipeline runs automatically and the user
gets one ending note.

**Independent Test**: add a chat whose fake own history has 30 messages in the last 6 months.
Check that a new global profile version is built, that the contact gets a profile, and that
the Rebuild result note is sent. A chat with no own messages gives "nothing to learn" and no
build.

### Tests for User Story 2

- [X] T017 [P] [US2] Write the integration tests for the profile update. File: tests/integration/test_add_chats_flow.py. Use the same fake LLM provider setup as tests/integration/test_us4_profiles.py. The cases are:
  - **Success**: 30 own messages in `own_history` for the new chat. Expect a new current global profile version, a contact profile for the chat, `contacts.own_message_count_6m == 30` and a `profile.rebuilt` note. There is no `profile.rebuilding` message.
  - **Nothing to learn**: the new chat has 0 own messages. Expect `chats.add_nothing_to_learn`, the global profile version unchanged, and no LLM call.
  - **Failure**: the LLM raises. Expect `chats.add_learn_failed`, the chats still enabled and the profiles unchanged.
  - **Lock held**: with `profile_build_lock(user.id)` held, the update waits and runs after release, with one note.
  - **Deleted meanwhile**: the user is deleted during the update. No message.
  - **Undo**: the latest refresh's ↩️ Undo is superseded after a successful update (FR-016).

### Implementation for User Story 2

- [X] T018 [US2] At the end of `finish_add`, when ≥ 1 chat was created, call `profile_command.rebuild_profiles(..., wait=True, only_if_new_in={created telegram_chat_ids}, announce=False, failure_key="chats.add_learn_failed")`. It runs after the confirmation and the `/chats` list are sent, and after the state is cleared, so other commands are answered meanwhile (research R3). The handler needs `settings`, `llm_router`, `gateway` and `clock` from the workflow data. File: src/scribe/telegram/control_bot/add_chats.py.
- [X] T019 [US2] Make sure `chats.added` says that learning runs in the background and that a note follows, in en and ru. The note comes either way: the result, "nothing to learn" or the failure. Files: src/scribe/i18n/en.py and src/scribe/i18n/ru.py (adjust the T002 texts if needed).

**Checkpoint**: US1 + US2 cover the issue's main acceptance criteria: drafts in new chats,
and style learned from them.

---

## Phase 5: User Story 3 - Cancel, already-added chats and unavailable Telegram (Priority: P2)

**Goal**: every failure or exit path leaves the data unchanged and answers clearly.

**Independent Test**: check each of these paths in turn: `/cancel`, `/settings` and `/help`
during the picker, a stale picker tap, a tap with no picker open, a disconnected session,
`fail_list_dialogs`, and all chats already added. After each one there are no new rows and
no state.

### Tests for User Story 3

- [X] T020 [P] [US3] Write the integration tests for leaving the picker. File: tests/integration/test_add_chats_flow.py. The cases are:
  - `/cancel` gives `chats.add_cancelled` and clears the state.
  - `/settings` while picking gives the settings view, and the state is cleared.
  - `/help` while picking gives the help text, and the state is cleared.
  - Free text gives `chats.add_hint`.
  - A `pickchat:` tap on an older picker message (different `message_id`) answers `alert.picker_closed` and leaves the selection unchanged.
  - A `pickdone:` tap with no state answers `alert.picker_closed`, with no rows.
  - A stale setup picker tap after setup answers `alert.picker_closed`.
- [X] T021 [P] [US3] Write the integration tests for Telegram problems. File: tests/integration/test_add_chats_flow.py. The cases are:
  - A telegram session row with status not `active`, or no row, gives `telegram.disconnected_start` with the reconnect keyboard.
  - `fail_list_dialogs = GatewayNotStarted()` (and a generic `RuntimeError`) gives `common.send_to_continue` with `connect.telegram_unavailable` and `/chats`.
  - Every dialog already a work chat gives `chats.add_none_left`.

  After each case there is no FSM state and no new `work_chats` rows.

### Implementation for User Story 3

- [X] T022 [US3] Add the Telegram checks to `add_chats_callback` before listing the dialogs. File: src/scribe/telegram/control_bot/add_chats.py.
  - Use `users_repo.get_telegram_session`. When it is not `active`, answer `telegram.disconnected_start` + `reconnect_keyboard(lang)`.
  - Wrap `list_dialogs` in `try`/`except Exception`. On failure, log `add_chats_list_dialogs_failed` with the error type only and answer `common.send_to_continue(problem=t("connect.telegram_unavailable"), command="/chats")` (research R7).
- [X] T023 [US3] Add the exit handlers to the add router. File: src/scribe/telegram/control_bot/add_chats.py.
  - `/cancel` under `StateFilter(AddChatsStates.picking)` clears the state, removes the picker's buttons (best effort) and answers `chats.add_cancelled`.
  - Free text that does not start with `/` answers `chats.add_hint`.
- [X] T024 [US3] Implement `AbandonedPickerMiddleware` in src/scribe/telegram/control_bot/chat_picker.py. When the state is `AddChatsStates.picking` and the message is a command other than `/cancel`, it clears the state and lets the command through. Register it as a dispatcher-level `message.outer_middleware` after the access gate in src/scribe/telegram/control_bot/bot.py, so `/help` and `/start` are covered too (research R5).

**Checkpoint**: all failure and exit paths are safe.

---

## Phase 6: User Story 4 - Adding is recorded in /history (Priority: P3)

**Goal**: "➕ Work chats added · titles" in `/history`, with ids only stored.

**Independent Test**: add two chats, then check that `/history` shows one line with both
titles and that the record's `meta_json` holds ids only.

### Tests for User Story 4

- [X] T025 [P] [US4] Write the tests for the history record. File: tests/integration/test_add_chats_flow.py, plus tests/unit/test_i18n_render.py for the label. They check that:
  - a Done that added 2 chats writes one `chats_added` record with `meta_json == {"work_chat_ids": [<2 uuids>]}` and no titles;
  - a Done that added 0 chats writes none;
  - `/history` renders `➕ Work chats added · A, B`;
  - 5 chats render 3 titles and `+2 more`;
  - a deleted chat id is skipped;
  - the Russian rendering works.

### Implementation for User Story 4

- [X] T026 [US4] Write the `activity_repo.record(session, user_id, ActivityKind.chats_added, at=clock(), meta={"work_chat_ids": [str(id) …]})` call in `finish_add`, in the same transaction as `add_work_chats`, only when ≥ 1 row was created. File: src/scribe/telegram/control_bot/add_chats.py.
- [X] T027 [US4] Render the record in `/history`. File: src/scribe/telegram/control_bot/settings.py.
  - `_recent_activity` (or `history_command`) loads the titles for the `work_chat_ids` of the `chats_added` records on the page, in one query scoped by `user_id`.
  - `history_line` renders `history.kind.chats_added` · up to 3 titles + `history.more`, using `common.chat` for an empty title.

**Checkpoint**: all four stories are complete.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [X] T028 [P] Add a test that the access gate applies. With `access_gate` on, a suspended user's `chatadd:0` and `pickdone:0` get the gate answer, and no rows are written. A user with access proceeds. File: tests/integration/test_add_chats_flow.py (reuse helpers from tests/integration/test_access_gate_bot.py).
- [X] T029 [P] Update the 001 documents. File: specs/001-telegram-reply-agent/contracts/control-chat.md:
  - the Commands table row for `/chats`;
  - the `/chats` bullet in "Settings", which links to specs/007-add-work-chats/contracts/control-chat.md;
  - the `/history` list (`➕ Work chats added`).

  File: specs/001-telegram-reply-agent/spec.md:
  - FR-024: users can add work chats after setup from `/chats`, through the same picker, and the new chats' own messages are learned as on Rebuild (spec 007);
  - FR-022: the same note.
- [X] T030 [P] Update the module docstrings. Files: src/scribe/telegram/control_bot/setup_flow.py (the picker now lives in chat_picker.py) and src/scribe/telegram/control_bot/settings.py (the `chatadd` callback in the list of callbacks).
- [X] T031 Run `make lint`, `make typecheck`, `make test-fast` and `make test-integration`, and fix any findings. Run through quickstart.md "Automated".
- [ ] T032 Ask the operator to run quickstart.md "Manual" on the staging bot, with a backup before `alembic upgrade head`. Do not touch the hosted instance from the agent.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies. T001 and T002 run in parallel.
- **Foundational (Phase 2)**: T003 → T004. T005 → T006. T007 → T008 → T009. T010 and T011 are independent of the others. All of Phase 2 blocks the stories.
- **US1 (Phase 3)**: needs Phase 2. It is the MVP.
- **US2 (Phase 4)**: needs US1 (`finish_add`) and T011.
- **US3 (Phase 5)**: needs US1 (`add_chats_callback`, the router). It can run in parallel with US2, because the handlers live in different functions. Coordinate edits to add_chats.py.
- **US4 (Phase 6)**: needs US1 and T003/T004. It can run in parallel with US2 and US3.
- **Polish (Phase 7)**: after the desired stories.

### Within Each User Story

Write the tests first and make sure they fail, then implement. Each checkpoint is a working
increment.

### Parallel Opportunities

- T001 ∥ T002. T005 ∥ T007 ∥ T010 ∥ T011.
- T012 ∥ T013. T017, T020, T021 and T025 are separate test functions; write them in parallel
  only if they go to separate files, otherwise one after another.
- T029 ∥ T030.

## Parallel Example: Phase 2

```text
Task: "T005 Integration tests for chats_repo.add_work_chats"
Task: "T007 Unit tests for chat_picker.picker_keyboard / available_dialogs"
Task: "T011 Extract rebuild_profiles in profile_command.py"
```

## Implementation Strategy

### MVP First (User Story 1)

1. Phase 1 + Phase 2. Setup still works on the shared picker.
2. Phase 3. A chat can be added from `/chats`, and its new messages are drafted.
3. **Stop and validate**: run quickstart steps 1–3 and 5.

### Incremental Delivery

1. Add US2: profiles learn the new chats. This completes the issue's main acceptance
   criteria.
2. Add US3: safe exits and Telegram errors.
3. Add US4: the `/history` line.
4. Polish: the 001 documents, the gate test and the full suite.

### Risks

- **PR #31 conflict** in `profile_command.rebuild_callback`: T011 is isolated so it can be
  redone on top of #31.
- **Migration number**: 0012 (reserved); parallel branches use 0009–0011 and 0013 on top of 0008. Re-chain `down_revision` at merge (T004).
- **Spec 006** removes `group_mode` choices. T013 asserts only the default `mentions_only`
  behaviour, which holds before and after 006.
- **Spec 008 (Work account)** may change what the picker excludes. The exclusion set is a
  parameter of `available_dialogs` / `show_chat_picker`.
