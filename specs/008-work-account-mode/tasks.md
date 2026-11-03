---

description: "Task list for Work Account Mode (issue #15)"
---

# Tasks: Work Account Mode — Every Private Chat Is a Work Chat

**Input**: Design documents from `/specs/008-work-account-mode/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/control-chat.md,
contracts/internal-interfaces.md, quickstart.md

**Tests**: Included. Constitution Principle V requires automated tests for deterministic code,
written before or together with it. Write each story's tests first and see them fail.

- Bot-flow tests follow `tests/integration/test_control_bot.py` (`RecordingSession`,
  `offline_bot()`, `text_update` / `callback_update` from `tests/integration/bot_support.py`).
- Intake/outgoing tests follow the existing router and events integration tests with
  `FakeMessagingGateway` and `FrozenClock`; DB fixtures (`session_factory`, `make_user`,
  `committed_session_factory`) live in `tests/integration`.
- **Synthetic values only**: Telegram ids such as `900000101`, names such as "Test Contact A".
  No real message content in fixtures (Principle II).

**Organization**: grouped by user story (US1–US5 from spec.md) so each can be implemented and
tested on its own after the foundational phase.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: which user story the task belongs to

## Path Conventions

Single project: `src/scribe/`, `tests/`, `alembic/versions/` at the repository root.

## Ground Rules

- `work_chats.enabled` stays the only flag readers use (research R1). Every write of `origin`,
  `excluded`, or of `enabled` for an `auto` chat goes through `src/scribe/chats/work_account.py`.
- Lock order stays chat row → pending drafts → turns (`settings.disable_chat` protocol).
- Logs: ids and counts only; never message text or chat titles.
- Every user-facing string exists in `src/scribe/i18n/en.py` and `src/scribe/i18n/ru.py`.
- Migration revision: the next free number at implementation time (`0009` on today's main;
  check spec 006 / spec 007 branches first). Use `ADD VALUE IF NOT EXISTS` for enum values that
  spec 007 may also add (`chat_added`).

---

## Phase 1: Setup

**Purpose**: package skeleton and configuration.

- [X] T001 Create package `src/scribe/chats/__init__.py` and an empty module `src/scribe/chats/work_account.py` with the module docstring summarising research R1–R8 (invariant `origin = 'auto' → enabled = users.work_account AND NOT excluded`)
- [X] T002 [P] Add `work_account_import_max_chats: int = Field(default=50, ge=0)` to `src/scribe/config.py` with a comment pointing to spec FR-016, and cover it in `tests/unit/test_config.py`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: schema, gateway fields, eligibility and the shared deactivate protocol. All user
stories depend on this phase.

- [X] T003 Write migration `alembic/versions/00NN_work_account_mode.py` (down_revision = current head): first, in autocommit, `ALTER TYPE activity_kind ADD VALUE IF NOT EXISTS` for `work_account_on`, `work_account_off`, `chat_added`, `chat_excluded`, `chat_included`, and `ALTER TYPE dropped_reason ADD VALUE IF NOT EXISTS 'work_account_off'`; then create enum `work_chat_origin ('picked','auto')`, add `work_chats.origin work_chat_origin NOT NULL DEFAULT 'picked'`, `work_chats.excluded boolean NOT NULL DEFAULT false`, check constraint `work_chats_excluded_auto_only CHECK (origin = 'auto' OR NOT excluded)`, `users.work_account boolean NOT NULL DEFAULT false`, `users.work_account_changed_at timestamptz NULL`; downgrade drops columns/constraint/enum (enum values stay, as in 0008)
  - Note: Done as `alembic/versions/0013_work_account.py` (revision 0013, down_revision 0012 = spec 007). `chat_added` is not added: pick-ups reuse 007's `chats_added` kind with meta `origin: "auto"`.
- [X] T004 Update `src/scribe/db/models.py`: `WorkChatOrigin(StrEnum)` (`picked`, `auto`); `WorkChat.origin` (default `picked`, server default `'picked'`), `WorkChat.excluded` (default false, server default false) and the check constraint in `__table_args__`; `User.work_account` (default false, server default false), `User.work_account_changed_at`; new `ActivityKind` values and `DroppedReason.work_account_off`
- [X] T005 [P] Add migration/model round-trip assertions (defaults for existing rows are `picked`/`false`; check constraint rejects `origin='picked', excluded=true`) in `tests/integration/test_models_and_repos.py`
- [X] T006 [P] Add `PeerKind = Literal["user","bot","self","service","deleted"]`, `IncomingMessage.peer_kind: PeerKind | None = None`, and `OutgoingEvent.chat_kind: DialogKind = "private"`, `OutgoingEvent.peer_kind: PeerKind | None = None`, `OutgoingEvent.chat_title: str = ""` in `src/scribe/telegram/gateway.py` (docstring: `None` is never eligible)
- [X] T007 [P] Write unit tests for the Telethon mapping in `tests/unit/test_user_client.py`: private `User` entity → `user`; `bot=True` → `bot`; `is_self` → `self`; `deleted=True` → `deleted`; `support=True` or id `777000` → `service`; groups → `None`, for both incoming and outgoing; `dialog_kind` returns `None` for deleted and service accounts
- [X] T008 Implement `peer_kind_of(entity)` and fill `peer_kind` in `_handle_new_message` (incoming: from `event.get_chat()` when `event.is_private`; outgoing: set `chat_kind`, `peer_kind`, `chat_title` via `event.is_private` / `event.get_chat()`), and make `dialog_kind` drop deleted/service accounts, in `src/scribe/telegram/user_client.py` (depends on T006, T007)
- [X] T009 [P] Let `FakeMessagingGateway` build incoming/outgoing events with the new fields and dialogs carry an optional peer kind in `src/scribe/telegram/fake_gateway.py`
  - Note: No change needed: the new DTO fields have defaults, and `list_dialogs` already leaves out ineligible private dialogs, so fake dialogs need no peer kind.
- [X] T010 [P] Write `tests/unit/test_work_account_eligibility.py`: `is_eligible("private","user")` is True; every other `peer_kind` (incl. `None`) and every group/channel kind is False
- [X] T011 Implement pure `is_eligible(chat_kind, peer_kind)` in `src/scribe/chats/work_account.py` (depends on T010)
- [X] T012 Move the disable protocol from `src/scribe/telegram/control_bot/settings.py` (`_disable_chat_once`, `_DraftAppeared`, retry loop) to `deactivate_chat(session_factory, user_id, work_chat_id, now, *, reason: DroppedReason, set_excluded: bool | None = None) -> list[UUID] | None` in `src/scribe/chats/work_account.py`; keep `settings.disable_chat` as a thin wrapper with `reason=DroppedReason.chat_disabled`; existing disable tests in `tests/integration/test_control_bot.py` must stay green unchanged
- [X] T013 Add repo helpers in `src/scribe/db/repos/chats.py`: `create_auto_chat(session, user_id, *, telegram_chat_id, title, kind, enabled, excluded)` (insert … `ON CONFLICT (user_id, telegram_chat_id) DO NOTHING` then select), `count_excluded(session, user_id)` (`kind='private' AND ((origin='auto' AND excluded) OR (origin='picked' AND NOT enabled))`), `count_paused(session, user_id)` (`origin='auto' AND NOT excluded AND NOT enabled`), and `list_work_chats(..., include_auto: bool = True, offset: int = 0, limit: int | None = None)` keeping current defaults
  - Note: `list_work_chats` gained `include_auto`; `/chats` pages in Python (no `offset`/`limit`). Also `count_auto` and `is_excluded`. `create_auto_chat` returns `(row, created)`.
- [X] T014 [P] Add i18n keys skeleton (en + ru) for every text in `contracts/control-chat.md` to `src/scribe/i18n/en.py` and `src/scribe/i18n/ru.py`, including `history.kind.work_account_on/off`, `history.kind.chat_added`, `history.kind.chat_excluded`, `history.kind.chat_included`, `status.work_account_off`; `tests/unit/test_i18n_lint.py` must pass

**Checkpoint**: schema, gateway flags, eligibility and shared deactivate in place; behaviour for
users with the mode off is unchanged (full suite green).

---

## Phase 3: User Story 1 — Turn on work account mode in setup and get drafts from everyone (Priority: P1) 🎯 MVP

**Goal**: with the mode switched on in the setup picker, every eligible private chat — existing
or first-time — gets drafts; bots, Saved Messages, service accounts and unpicked groups never.

**Independent Test**: setup with the mode on and nothing ticked; an unpicked existing contact
and a brand-new contact both get drafts; a bot and an unpicked group produce nothing stored.

### Tests for User Story 1

- [X] T015 [P] [US1] Write `tests/integration/test_work_account_intake.py`: mode on + unknown eligible private chat → `auto` enabled row, `chat_added` activity (meta `work_chat_id`, `origin: "auto"`), message stored, `new_turn`; first-time contact same; `peer_kind` bot/self/service/deleted/None and unpicked group → `Action.ignore`, no `messages`/`contacts`/`work_chats` rows; mode off → ignore as today; two concurrent first messages (two tasks on the same chat) → one row, one turn; redelivered message → `duplicate`
  - Note: Pick-ups are recorded as `chats_added` (007's kind) with meta `{work_chat_ids: [id], origin: "auto"}`.
- [X] T016 [P] [US1] Extend `tests/integration/test_work_account_intake.py` with outgoing-first: user writes first to an unknown eligible chat with the mode on → `auto` row and stored outgoing message; the contact's answer opens a turn whose history contains the outgoing message (spec US1 scenario 7); outgoing to Saved Messages / a bot → nothing stored
- [X] T017 [P] [US1] Write `tests/unit/test_chat_picker.py`: mode-off keyboard identical to today's `picker_keyboard`; mode-on keyboard shows the `pickwa:off` switch row, private dialogs ✅ unless excluded (🚫), groups ✅/⬜ by selection, Done label "all private chats, N excluded, G groups"; paging unchanged; all callback data ≤ 64 bytes
- [X] T018 [P] [US1] Write setup-flow cases in `tests/integration/test_work_account_bot.py`: `pickwa:on` redraws with the explanation; `pickchat` on a private dialog toggles exclusion with the mode on and selection with it off; Done with nothing ticked and the mode on proceeds to the time-zone step and saves `users.work_account=true`, `work_account_changed_at`, `auto` excluded rows for excluded dialogs, `picked` rows for ticked groups; Done with nothing ticked and the mode off still shows `setup.pick_none`; confirmation text in en and ru

### Implementation for User Story 1

- [X] T019 [US1] Implement `pick_up(session, user_id, *, telegram_chat_id, title, chat_kind, peer_kind, direction, now) -> WorkChat | None` in `src/scribe/chats/work_account.py`: return None unless `is_eligible`; read `users.work_account` with `SELECT … FOR SHARE`; if on, `chats_repo.create_auto_chat(enabled=True, excluded=False)`, record `ActivityKind.chat_added` (meta `{"work_chat_id": str, "origin": "auto"}`), log `work_chat_picked_up` (user_id, work_chat_id, direction); log `work_account_ineligible` at debug with `peer_kind` only (depends on T011, T013)
- [X] T020 [US1] In `IntakeRouter._handle` (`src/scribe/intake/router.py`), when `get_work_chat_by_telegram_id` returns None and the message is incoming, call `work_account.pick_up(..., direction="in", title=message.chat_title or message.sender_display_name)`; on a row continue the existing path; keep the pure `decide()` and `ChatRule` unchanged; update the module docstring (008 pick-up)
- [X] T021 [US1] In `src/scribe/intake/events.py`, at both `chat is None` sites of outgoing handling, call `work_account.pick_up(..., direction="out", chat_kind=event.chat_kind, peer_kind=event.peer_kind, title=event.chat_title)` and continue with the new row (nothing to drop; the outgoing message is stored); update the module docstring
- [X] T022 [US1] Create `src/scribe/telegram/control_bot/chat_picker.py`: move `picker_keyboard`, `PAGE_SIZE`, `TITLE_CHARS` from `setup_flow.py` and extend with `work_account: bool` and `excluded: set[int]` per contract; keep `setup_flow.picker_keyboard` importable (re-export) for spec 007 and existing tests (depends on T017)
  - Note: Adapted: 007 already created `chat_picker.py`; extended it (`work_account` switch, `excluded`, `exclusions_only`, `kinds`, `pickwa:` handler) instead of creating it.
- [X] T023 [US1] In `src/scribe/telegram/control_bot/setup_flow.py`: store `work_account` and `excluded` in the picker FSM data; add `pick_work_account` handler for `pickwa:<on|off>` (registered under `SetupStates.pick_chats`); make `pick_toggle` toggle exclusion for private dialogs while the mode is on (accept ids only if present in `data["dialogs"]`); in `pick_done` allow an empty selection when the mode is on, save `users.work_account` + `work_account_changed_at`, upsert ticked dialogs as `picked` enabled rows, create `auto` excluded rows for excluded dialogs via `work_account.exclude_chat(dialog=…)` (record `work_account_on` and `chat_excluded`), and send `setup.chats_saved_work_account` (depends on T019, T022)
- [X] T024 [US1] Fill the US1 i18n texts (picker explanation, switch labels, Done label, confirmation) in `src/scribe/i18n/en.py` and `src/scribe/i18n/ru.py`

**Checkpoint**: issue #15 acceptance "a message from a contact unknown at setup time is handled
when the checkbox is on" passes from setup.

---

## Phase 4: User Story 2 — Exclude individual chats (Priority: P1)

**Goal**: excluded chats are never read, stored or drafted while the mode is on; exclusion
drops their waiting messages and pending drafts; include again works; previously disabled
private chats count as excluded.

**Independent Test**: exclude one chat that wrote before and one that never did; both produce
nothing; include one again and its next message gets a draft.

### Tests for User Story 2

- [X] T025 [P] [US2] Add to `tests/integration/test_work_account_switch.py`: `exclude_chat` on an `auto` chat sets `excluded=true, enabled=false`, drops waiting turns and pending drafts with `chat_disabled`, records `chat_excluded`; with `dialog=` and no row creates an `auto` excluded row with no contact and no messages; on a `picked` chat equals disable; `include_chat` on `auto` sets `excluded=false, enabled=users.work_account`, on `picked` sets `enabled=true`, records `chat_included`; a draft being composed in a chat excluded meanwhile is not stored (composer `FOR SHARE` path)
- [X] T026 [P] [US2] Add to `tests/integration/test_work_account_intake.py`: messages in an excluded chat (row existing before and created by exclusion) → ignore, nothing stored; after include, the next message opens a turn and messages from while it was excluded are not drafted; a `picked` private chat disabled before the mode was turned on stays ignored and counts in `count_excluded`
- [X] T027 [P] [US2] Add chat-screen cases to `tests/integration/test_work_account_bot.py`: with the mode on a private chat's screen shows "🚫 Exclude" / "↩️ Include again" and the origin status line; pressing them calls exclude/include, updates dropped draft cards, and re-renders; a group's screen is unchanged

### Implementation for User Story 2

- [X] T028 [US2] Implement `exclude_chat(session_factory, user_id, *, work_chat_id=None, dialog=None, now) -> list[UUID]` and `include_chat(session_factory, user_id, work_chat_id, *, now)` in `src/scribe/chats/work_account.py` using `deactivate_chat(reason=DroppedReason.chat_disabled, set_excluded=True)` for `auto` and the plain disable for `picked`; log `work_chat_excluded` / `work_chat_included` (user_id, work_chat_id, origin) (depends on T012, T013)
- [X] T029 [US2] In `src/scribe/telegram/control_bot/settings.py`: `chat_settings_text` gains the origin status line for private chats while the mode is on; `chat_settings_keyboard` takes the user's mode and labels the enable row `btn.chat_exclude` / `btn.chat_include` for private chats while on; `chat_option_callback` routes `chatenable` for private chats while the mode is on to `exclude_chat` / `include_chat` (cards of dropped drafts get `status.chat_excluded` for `auto` chats, existing `status.chat_disabled` otherwise)
- [X] T030 [US2] Fill the US2 i18n texts (`btn.chat_exclude`, `btn.chat_include`, `settings.chat_origin.*`, `status.chat_excluded`) in `src/scribe/i18n/en.py` and `src/scribe/i18n/ru.py`

**Checkpoint**: issue #15 acceptance "a chat excluded by hand is not handled even with the
checkbox on" passes.

---

## Phase 5: User Story 3 — See and switch the mode in /settings (Priority: P1)

**Goal**: `/settings` shows the mode and excluded count; turning on/off is confirmed; off goes
back to the hand-picked list; on again restores exclusions.

**Independent Test**: from a normal setup, turn the mode on in `/settings` → an unpicked contact
drafts; turn it off → that contact does not, hand-picked chats still do; on again → previous
exclusions apply.

### Tests for User Story 3

- [X] T031 [P] [US3] Add to `tests/integration/test_work_account_switch.py`: `switch(on=True)` sets the flag + `work_account_changed_at`, re-enables only `auto` non-excluded chats, records `work_account_on` once, returns `changed=False` when already on; `switch(on=False)` commits the flag first, pauses every enabled `auto` chat via `deactivate_chat(reason=work_account_off)`, drops their waiting turns and pending drafts, leaves `picked` chats untouched, records `work_account_off`; on → off → on restores the same exclusions (SC-006); a `pick_up` that holds the user row `FOR SHARE` before the switch is swept; a `pick_up` after the switch commits sees the mode off; `reconcile_user` fixes rows left half-swept; `preview_off` counts chats, pending drafts and waiting turns
- [X] T032 [P] [US3] Add `/settings` cases to `tests/integration/test_work_account_bot.py`: state line "off — only the chats you chose" / "on — every private chat, N excluded"; `settings:work_account` sends the matching confirmation with counts; `wacct:on` / `wacct:off` apply and re-render; `wacct:cancel` changes nothing; a stale `wacct:on` when already on shows the state and changes nothing; dropped draft cards show `status.work_account_off`; en and ru

### Implementation for User Story 3

- [X] T033 [US3] Implement `switch(session_factory, user_id, *, on, now) -> SwitchResult`, `preview_off(session, user_id) -> OffPreview`, and `reconcile_user(session_factory, user_id, *, now) -> int` in `src/scribe/chats/work_account.py` per research R4/R8 (flag update under `SELECT users … FOR UPDATE` committed first; per-chat `deactivate_chat` in id order for off; one `UPDATE work_chats SET enabled = true WHERE origin='auto' AND NOT excluded` for on); log `work_account_switched` and `work_account_reconciled` (depends on T012, T028)
- [X] T034 [US3] In `src/scribe/telegram/control_bot/settings.py`: `settings_text` gains the work account line (with `count_excluded`); `settings_keyboard` gains the `settings:work_account` row; `settings_menu` handles `work_account` by sending the on/off confirmation (`preview_off` counts for off); new `work_account_callback` for `wacct:<on|off|cancel>` calls `switch`, updates dropped draft cards via `DraftPresenter.show_status(..., Msg("status.work_account_off"))`, answers and re-renders `/settings`; register `wacct:` in `create_router()`; update the module docstring's callback list
- [X] T035 [US3] At worker start in `src/scribe/__main__.py`, run `work_account.reconcile_user` for users whose `work_account_changed_at` is within the last 24 h (research R8); log the count fixed
- [X] T036 [US3] Fill the US3 i18n texts (`settings.work_account.*`, `btn.work_account_on/off`, `settings.work_account_confirm_on/off`, `btn.wacct_*`, results) in `src/scribe/i18n/en.py` and `src/scribe/i18n/ru.py`

**Checkpoint**: all four acceptance criteria of issue #15 pass.

---

## Phase 6: User Story 4 — Picked-up chats are visible and configurable (Priority: P2)

**Goal**: `/chats` header, marks, paging, mode-off summary, exclusion picker; `/history` and
`/my_data` show the mode and its chats.

**Independent Test**: three new contacts write with the mode on; `/chats` lists them marked
`· 💼`, pages past 8, the exclusion picker offers a never-written chat; `/history` shows the
pick-ups.

### Tests for User Story 4

- [X] T037 [P] [US4] Add `/chats` cases to `tests/integration/test_work_account_bot.py`: header line with excluded count while on; marks ⚙️ / ⏸ / 🚫 and `· 💼`; 20 chats → 3 pages via `chatspage:<n>`; mode off hides `auto` chats and shows the paused summary; empty list with the mode on shows the header and `wxopen` instead of `chats.none`
- [X] T038 [P] [US4] Add exclusion-picker cases to `tests/integration/test_work_account_bot.py`: `wxopen:0` lists eligible private dialogs from the fake gateway with current state (incl. a dialog with no row); `wxpick` with an id not in the FSM dialog list is ignored; `wxdone:0` applies exclusions/inclusions and answers with counts; Telegram unavailable → `connect.telegram_unavailable`, nothing changes; `/cancel` closes without changes
  - Note: The exclusion picker reuses the shared picker's `pickchat` / `pickpage` / `pickdone` callbacks (purpose `exclude`, `src/scribe/telegram/control_bot/exclusions.py`) instead of new `wxpick` / `wxpage` / `wxdone`; `wxopen:0` opens it.
- [X] T039 [P] [US4] Add `/history` and `/my_data` cases: `chat_added` / `chat_excluded` / `chat_included` show the chat title looked up by `meta.work_chat_id`; `work_account_on/off` lines; `/my_data` summary shows mode, picked-up and excluded counts; export JSON has `work_account`, `work_account_changed_at`, per-chat `origin` and `excluded`; `/delete_my_data` removes everything — in `tests/integration/test_retention.py` and `tests/integration/test_work_account_bot.py`

### Implementation for User Story 4

- [X] T040 [US4] Rework `chats_command` in `src/scribe/telegram/control_bot/settings.py`: header line, marks, order (enabled first, then title), paging (`PAGE_SIZE` from `chat_picker.py`, new `chatspage:<n>` callback editing the message), mode-off hidden `auto` chats with the `count_paused` footer, `wxopen:0` button while the mode is on; register callbacks in `create_router()`
- [X] T041 [US4] Implement the exclusion picker in `src/scribe/telegram/control_bot/settings.py` (or a new `src/scribe/telegram/control_bot/exclusions.py` included in the settings router): `ChatsStates.exclude` with FSM data `{dialogs, excluded, page}` built from `gateway.list_dialogs` filtered by eligibility and joined with `work_chats` rows; `wxpick`, `wxpage`, `wxdone` handlers using `chat_picker.picker_keyboard`; on Done call `exclude_chat` / `include_chat` for changed dialogs; `/cancel` support; Telegram-unavailable path
- [X] T042 [US4] `/history` rendering in `src/scribe/telegram/control_bot/settings.py`: `_history_label` for the new kinds and `_recent_activity` resolving the chat title from `meta.work_chat_id` (scoped by `user_id`) for the contact column
- [X] T043 [US4] `/my_data` summary and export fields in `src/scribe/retention/user_data.py` (mode, changed_at, picked-up count, excluded count; per-chat `origin`, `excluded`)
- [X] T044 [US4] Fill the US4 i18n texts (`chats.header_work_account`, `chats.paused_footer`, `btn.exclude_chats`, exclusion picker texts, history labels, `/my_data` lines) in `src/scribe/i18n/en.py` and `src/scribe/i18n/ru.py`

**Checkpoint**: "settings show that 'all chats' mode is on and how many chats are excluded" and
the transparency gap seen in testing are closed.

---

## Phase 7: User Story 5 — Style learning includes the work account's chats (Priority: P3)

**Goal**: setup import and 🔄 Rebuild read own messages from enabled chats plus up to
`work_account_import_max_chats` recent eligible private dialogs while the mode is on; imported
chats become `auto` so they rank for contact profiles.

**Independent Test**: set up with the mode on and nothing picked on an account with several
private chats with own messages; "learned from N messages" counts them; an excluded chat
contributes nothing.

### Tests for User Story 5

- [X] T045 [P] [US5] Write `tests/integration/test_work_account_import.py`: `import_targets` with the mode off = enabled work chats only; with the mode on adds the most recent eligible private dialogs without a row or with an enabled row, capped at `work_account_import_max_chats` (test with cap 3 and 5 dialogs), never excluded chats, paused chats or unpicked groups; `materialise_imported` creates `auto` enabled rows (`chat_added`) only for dialogs with ≥ 1 own message; setup and Rebuild pass the targets and the per-contact ranking sees the new chats (`own_message_count_6m`) when `build_profiles_from_import` runs

### Implementation for User Story 5

- [X] T046 [US5] Implement `import_targets(session, gateway, user_id, *, max_auto)` and `materialise_imported(session, user_id, imported, targets, *, now)` in `src/scribe/chats/work_account.py` (research R6)
- [X] T047 [US5] Add optional `targets: list[ImportTarget] | None = None` to `import_own_messages` in `src/scribe/style/importer.py` (None = today's behaviour) and log `auto_targets` in `style_import_finished`
- [X] T048 [US5] Use the targets and materialisation in `_import_and_finish` of `src/scribe/telegram/control_bot/setup_flow.py` and in 🔄 Rebuild in `src/scribe/telegram/control_bot/profile_command.py` (call `materialise_imported` and commit before building profiles; keep whichever builder is current on main — `build_global_profile` or `build_profiles_from_import` after issue #29)
  - Note: Setup keeps `build_global_profile` (current builder at setup); Rebuild uses `build_profiles_from_import`.

**Checkpoint**: all user stories functional.

---

## Phase 8: Polish & Cross-Cutting Concerns

- [X] T049 [P] Update `specs/001-telegram-reply-agent/contracts/control-chat.md` (setup `pick_chats`, `/settings`, `/chats`) and add a note to 001 FR-001 in `specs/001-telegram-reply-agent/spec.md` pointing to spec 008
- [X] T050 [P] Update `README.md` / `README.ru.md` feature list with work account mode (one line each, if the READMEs list features)
- [X] T051 [P] Log-redaction check: extend `tests/unit/test_logging_redaction.py` (or the intake log tests) to assert the new log events carry no `title`/`text` fields
- [ ] T052 (automated part done: full suite, ruff and mypy green locally; the manual quickstart on a test account is a manual follow-up) Run the full suite (`uv run pytest`), `ruff`, and the type checker; run the quickstart manual steps on a local/staging instance with a test account (never the hosted run directory without the operator)
- [ ] T053 (not run in this implementation session; follow-up) Run `/speckit-analyze` for spec/plan/tasks consistency and fix findings

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: none.
- **Foundational (Phase 2)**: after Setup; blocks all stories. T003 → T004 → T005; T006 → T007 →
  T008; T010 → T011; T012 and T013 after T004.
- **US1 (Phase 3)**: after Phase 2. MVP.
- **US2 (Phase 4)**: after Phase 2; T028 is used by US1's T023 for setup exclusions, so in
  practice implement T028 before T023 (or stub exclusions in T023 and wire them in Phase 4).
- **US3 (Phase 5)**: after Phase 2 and T028 (switch reuses exclude/deactivate).
- **US4 (Phase 6)**: after US2 (exclusion picker uses `exclude_chat` / `include_chat`) and T022
  (shared picker).
- **US5 (Phase 7)**: after Phase 2 and US1's pick-up semantics (T019); independent of US3/US4.
- **Polish (Phase 8)**: after the stories in scope.

### Within Each Story

Tests first (fail) → `work_account.py` functions → handlers/UI → i18n texts.

### Parallel Opportunities

- Phase 2: T005, T006, T009, T010, T014 in parallel after T004 where noted.
- US1: T015, T016, T017, T018 in parallel (different test files or independent sections).
- US2: T025, T026, T027 in parallel.
- US3: T031, T032 in parallel.
- US4: T037, T038, T039 in parallel.
- US5 can proceed in parallel with US3/US4 by a second developer.

## Parallel Example: User Story 1

```text
Task: "T015 intake pick-up tests in tests/integration/test_work_account_intake.py"
Task: "T017 picker keyboard tests in tests/unit/test_chat_picker.py"
Task: "T018 setup-flow tests in tests/integration/test_work_account_bot.py"
```

## Implementation Strategy

### MVP First (User Story 1, plus T028)

1. Phase 1 + Phase 2.
2. US1 with `exclude_chat` (T028) available for the setup picker's exclusions.
3. Validate: unknown and first-time contacts draft; bots/groups ignored. Deploy to the test server
   (mode is off by default).

### Incremental Delivery

1. US1 → setup users can opt in.
2. US2 → exclusions from chat screens (closes the privacy gap for existing chats).
3. US3 → existing users switch in `/settings`; off returns to the hand-picked list (all issue
   acceptance criteria).
4. US4 → `/chats` scale and transparency.
5. US5 → style import covers the work account.

### Coordination

- Rebase on spec 006 / spec 007 if they land first: `chat_settings_keyboard` (006 removes the
  group-mode row), `chat_picker.py` (007 may create it; reuse instead of duplicating),
  migration number, `chat_added` activity kind.
