---

description: "Task list for 006 — group chats draft only when addressed"
---

# Tasks: Group Chats — Draft Only When Addressed

**Input**: Design documents from `/specs/006-group-mention-only/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: Required. The constitution (Principle V) asks for automated tests for deterministic
code, written before or with the code. Each story writes its tests first and confirms they
FAIL before implementing.

**Organization**: US1 changes the intake rule, so groups formerly set to `always` already
follow it once the router stops reading `group_mode`. US2 removes the mode from schema, UI and
texts. Both are P1; US1 is the MVP.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: US1, US2 (spec.md user stories)

---

## Phase 1: Setup

**Purpose**: Baseline before changes

- [X] T001 Confirm the baseline: `git fetch origin main`, Alembic head is `0008` (`alembic/versions/0008_skip_closing_messages.py`; if `main` has a newer head, note it for T019), and `make lint typecheck test-fast` passes on the branch

---

## Phase 2: Foundational

**Purpose**: None blocking. Both stories touch different layers (US1: intake and Telegram
mapping; US2: schema, control chat and i18n). The only shared point is `ChatRule`, which US1
changes first.

**Checkpoint**: Baseline green; user story work can begin.

---

## Phase 3: User Story 1 - A draft appears only when I am addressed in a group (Priority: P1) 🎯 MVP

**Goal**: In groups, a turn opens only for a message that tags the user or replies to one of
the user's messages. Follow-ups in the grouping window still join. Every intake decision logs
its reason. No chat setting can override this.

**Independent Test**: In a group work chat (including one stored with `group_mode='always'`),
send a tag, a reply without a tag, and a plain message. Exactly two turns open, the plain
message is stored as context, and the `intake` log lines carry `reason` = `tagged` / `reply` /
`not_addressed`.

### Tests for User Story 1 (write first, must FAIL) ⚠️

- [X] T002 [P] [US1] Rewrite the group tests in `tests/unit/test_router.py` against the new contract (contracts/intake-rule.md). `ChatRule(enabled, kind)` has no `group_mode`. Drop `GroupMode` imports and replace `test_group_mentions_only_vs_always` with parametrized `address_reason` cases: private → `private`; group with `is_reply_to_me` → `reply`; group with `mentions_me` → `tagged`; both flags → `reply`; neither → `None`. Add `decide` cases asserting `IntakeDecision.reason`: plain group message → `store_only`/`not_addressed`; tag → `new_turn`/`tagged`; reply → `new_turn`/`reply`; unaddressed follow-up within the window → `attach`/`follow_up`; addressed follow-up → `attach`/`tagged`; unaddressed group media-only → `store_only`; addressed group media-only → `handle_manually`/`tagged`; private → `new_turn`/`private`
- [X] T003 [P] [US1] Add mapping-helper tests to `tests/unit/test_user_client.py` (research R3, contracts/intake-rule.md). `mentions_me` is False for `mentioned=True` with `fwd_from` set, and True for `mentioned=True` without `fwd_from` (covers both `MessageEntityMention` and `MessageEntityMentionName` messages, since the server flag drives it). In a group, a header `MessageReplyHeader(forum_topic=True, reply_to_msg_id=…, reply_to_top_id=None)` does not call `get_reply_message` and gives `is_reply_to_me=False`. A header with `reply_to_top_id` set and an `out=True` ordinary replied-to message gives `True`. A replied-to message with an `action` (service message, `out=True`) gives `False`. A header whose `reply_to_peer_id` is another peer gives `False`. Keep `test_group_reply_to_me_and_mentions` passing
- [X] T004 [P] [US1] Rewrite the group block of `tests/integration/test_us1_pipeline.py` (the `GroupMode.always` part near line 192). A chat created with `group_mode=GroupMode.always` (still a column in US1) gets an unaddressed message: no new turn. A reply (`is_reply_to_me=True`) without a tag opens a turn. A plain message stored earlier appears as history in the draft composed for a later tagged message in the same group (SC-004: assert on what the fake LLM provider received)
- [X] T005 [P] [US1] Add an intake-log test in `tests/integration/test_us1_pipeline.py` using `structlog.testing.capture_logs`. A group plain message logs `event="intake"`, `action="store_only"`, `reason="not_addressed"`. A tag logs `reason="tagged"`, a reply `reason="reply"`. No captured event contains the message text (FR-011)

### Implementation for User Story 1

- [X] T006 [US1] In `src/scribe/intake/router.py`: add `IntakeReason` (`private`, `reply`, `tagged`, `follow_up`, `not_addressed`), remove `group_mode` from `ChatRule` and `ChatRule.of`, and replace `is_addressed` with `address_reason(message, chat) -> IntakeReason | None` ("reply wins over tagged"). Add `reason: IntakeReason | None = None` to `IntakeDecision` and set it in `decide` per data-model.md "Intake rule". Keep the rest of `_handle` unchanged: it rebuilds `IntakeDecision` for `answered`/`attach_pending`/`attach_reading`/`new_turn`/`handle_manually`, so carry `reason` through those. Update the module docstring (no `always`). Makes T002 pass
- [X] T007 [US1] In `src/scribe/intake/router.py` `IntakeRouter.route`, add `reason=decision.reason.value if decision.reason else None` to the `intake` log line. Makes T005 pass (depends on T006)
- [X] T008 [P] [US1] In `src/scribe/telegram/user_client.py`, add pure helpers `mentions_me(message)` (`bool(message.mentioned) and message.fwd_from is None`), `replies_to_message(message)` (real `MessageReplyHeader` with `reply_to_msg_id`, not `forum_topic and reply_to_top_id is None`, and `reply_to_peer_id` None or this chat) and `is_own_message(reply)` (`out` and `action is None`). Use them in `message_to_incoming` and `_handle_new_message`, which calls `get_reply_message` only when `replies_to_message` is true. Makes T003 pass
- [X] T009 [P] [US1] Document the sharpened meaning of `mentions_me` / `is_reply_to_me` on `IncomingMessage` in `src/scribe/telegram/gateway.py` (comments only; fields unchanged; data-model.md table)
- [X] T010 [P] [US1] In `tests/integration/test_us2_review_fixes.py` `_group` (line ~538), stop relying on `group_mode=GroupMode.always`. Send the messages that must open or join turns as addressed (`mentions_me=True`), and keep Carol's "separate question" addressed so the test still covers two senders in one group. Update assertions only where the spec changes the behaviour: an unaddressed message from the turn's sender after the draft is pending no longer marks it outdated. Add one such case that asserts `not draft.outdated`
- [X] T011 [P] [US1] In `tests/integration/test_earlier_search_drafts.py` (line ~428), drop `group_mode=GroupMode.always` and make the incoming group message that triggers the draft addressed (`mentions_me=True`)
- [X] T012 [US1] Run `uv run pytest tests/unit/test_router.py tests/unit/test_user_client.py tests/integration/test_us1_pipeline.py tests/integration/test_us2_review_fixes.py tests/integration/test_earlier_search_drafts.py tests/integration/test_skip_flow.py tests/integration/test_us4_profiles.py -q`. All green

**Checkpoint**: US1 is fully functional. Groups formerly `always` already follow the rule
(FR-005 behaviourally), and the column is now unused.

---

## Phase 4: User Story 2 - No more "every message" mode for groups (Priority: P1)

**Goal**: The mode no longer exists anywhere. Settings and setup state the rule, old buttons
answer "no longer available", and the column and enum are dropped.

**Independent Test**: `/chats` → a group shows "👥 Drafts in this group: only when you are
tagged or replied to" and no group-mode buttons (en and ru). Pressing an old
`chatgroup:<id>:always` button changes nothing and shows "This option is no longer available".
After `alembic upgrade head`, a chat stored as `always` at 0008 follows the standard rule.

### Tests for User Story 2 (write first, must FAIL) ⚠️

- [X] T013 [P] [US2] Update `tests/unit/test_i18n_us34.py::test_russian_chat_settings`. Build `WorkChat` without `group_mode`. The expected line is "👥 Черновики в этой группе: только когда вас отметили или ответили на ваше сообщение". Assert that no button text "👥 Только упоминания" / "👥 Каждое сообщение" exists and that no callback starts with `chatgroup`. Add the same check for English ("👥 Drafts in this group: only when you are tagged or replied to")
- [X] T014 [P] [US2] Rewrite the settings block in `tests/integration/test_us4_account.py` (lines ~885–890). For a group chat, `chatgroup:<id>:always` and `chatgroup:<id>:mentions_only` callbacks answer `t("alert.option_removed")`, post no new settings message, and leave the row unchanged. Another user's `chatgroup:<id>:always` still gets `alert.chat_not_found` via `OwnershipMiddleware`. The group's settings keyboard has no `chatgroup` callback. Remove the `stored_chat.group_mode == bob_chat.group_mode` assertion (line ~265)
- [X] T015 [P] [US2] Add `test_migration_0009_drops_group_mode` to `tests/integration/test_jira_models.py`, using the existing `_alembic` scratch-database pattern. At `0008`, insert a work chat with `group_mode='always'` and one with `mentions_only`. Upgrade to `0009`: the column and the `group_mode` type are gone and both rows remain. Downgrade to `0008`: the column exists, NOT NULL, both rows are `mentions_only`, and there is no server default. Upgrade to `head` again
- [X] T016 [P] [US2] Add a check to `tests/unit/test_i18n_lint.py` (or extend `test_used_catalog_keys_exist`) that `settings.group_mode.always`, `settings.group_mode.mentions_only`, `btn.group_mentions_only` and `btn.group_always` are absent from both catalogs, and that `alert.option_removed` and `settings.chat_group` exist in en and ru with no `{mode}` placeholder

### Implementation for User Story 2

- [X] T017 [P] [US2] In `src/scribe/i18n/en.py` and `src/scribe/i18n/ru.py`, per contracts/control-chat.md: set `settings.chat_group` to the fixed texts, remove `settings.group_mode.*`, `btn.group_mentions_only` and `btn.group_always`, add `alert.option_removed` (en "This option is no longer available", ru "Эта настройка больше недоступна"), and append the group-rule sentence to `setup.pick_prompt` in both languages
- [X] T018 [US2] In `src/scribe/telegram/control_bot/settings.py`: `chat_settings_text` appends `t("settings.chat_group", lang)` for non-private chats without reading a mode, and `chat_settings_keyboard` drops the group-mode row. In `chat_option_callback`, any `chatgroup` action answers `t("alert.option_removed", lang)` and returns before opening a write session. Remove the `GroupMode` import, update the module and handler docstrings, and keep the `chatgroup` registration in `register` (stale buttons still route here). Keep `"chatgroup"` in `src/scribe/telegram/control_bot/ownership.py`. Makes T013, T014 pass (depends on T017)
- [X] T019 [US2] Create `alembic/versions/0009_drop_group_mode.py` with `revision="0009"` and `down_revision="0008"` (or the head found in T001). Upgrade: `op.drop_column("work_chats", "group_mode")`, then `sa.Enum(name="group_mode").drop(bind, checkfirst=True)`. Downgrade: create `sa.Enum("mentions_only", "always", name="group_mode")`, add the column `nullable=False, server_default="mentions_only"`, then `op.alter_column(..., server_default=None)`. The docstring states that downgrade cannot restore former `always` chats and that the app must be stopped before upgrading (research R4). Makes T015 pass
- [X] T020 [US2] In `src/scribe/db/models.py`, remove `class GroupMode` and `WorkChat.group_mode`. Remove the remaining `GroupMode` imports and `group_mode=` arguments in `tests/integration/test_us1_pipeline.py`, `tests/integration/test_us2_review_fixes.py`, `tests/integration/test_earlier_search_drafts.py`, `tests/integration/test_us4_account.py` and `tests/unit/test_i18n_us34.py`. `alembic check` (`tests/integration/test_models_and_repos.py`) must pass (depends on T019)
- [X] T021 [US2] Verify `grep -rn "GroupMode\|group_mode\|group_always\|group_mentions_only" src tests` matches only `alembic/versions/0001_initial.py`/`0009_drop_group_mode.py`, then run `make test-fast test-integration`

**Checkpoint**: Both stories are done. No way remains to draft for unaddressed group messages.

---

## Phase 5: Polish & Cross-Cutting Concerns

- [X] T022 [P] Update `specs/001-telegram-reply-agent/spec.md` (FR-010). FR-003 becomes "In group chats, System MUST create drafts only when the user is tagged or the message replies to one of the user's messages (see specs/006-group-mention-only)". Edit the matching Edge Case ("…no draft unless the user is tagged or replied to"). FR-024 drops group "drafting behavior", and the Work Chat key entity drops "group drafting behavior"
- [X] T023 [P] Update `specs/001-telegram-reply-agent/data-model.md`: remove the `group_mode` row, with a note "removed in 006, migration 0009". Update `specs/001-telegram-reply-agent/contracts/control-chat.md`: replace the "👥 Mentions only / 👥 Every message" bullet with the fixed group line plus the stale-`chatgroup` behaviour, and mark `chatgroup` as legacy in the callback list (line ~444)
- [X] T024 [P] Check user-facing docs for the removed mode (`README.md`, `README.ru.md`, `docs/`, `landing/`). Update any text that promises "every message" drafting in groups. `docs/earlier-search.md` concerns search and stays
- [X] T025 Run `make lint typecheck` and `make eval-dry`. No draft-generation change, so a full eval run is not required (Principle V)
- [ ] T026 Run quickstart.md: automated checks, then the live check on a test account (scenarios 1, 2, 3, 4, 6, 7), and record the scenario 6 `mentioned` finding in research.md R3. Apply the R3 fallback if the server flags plain topic posts
- [X] T027 In the PR description, state the deploy order (stop the app, `alembic upgrade head`, start the new image) and the silent behaviour change for former "every message" groups. If another branch merged `0009` first, re-chain `down_revision` and re-run T015

---

## Dependencies & Execution Order

- **Setup (T001)** → everything.
- **US1 (T002–T012)**: tests T002–T005 in parallel first. Then T006 → T007, with T008 and T009
  in parallel with T006. T010 and T011 can run in parallel after T006. T012 closes the phase.
- **US2 (T013–T021)** depends on US1's T006 (`ChatRule` no longer reads `group_mode`, so
  dropping the column cannot break intake). Tests T013–T016 run in parallel first. Then T017 →
  T018, and T019 → T020 → T021.
- **Polish (T022–T027)** after US2. T022–T024 run in parallel.

### Parallel examples

```text
US1 tests:  T002 (test_router.py) | T003 (test_user_client.py) | T004+T005 (test_us1_pipeline.py, one author)
US1 impl:   T006 (router.py) | T008 (user_client.py) | T009 (gateway.py)
US2 tests:  T013 | T014 | T015 | T016
Polish:     T022 | T023 | T024
```

## Implementation Strategy

1. **MVP = US1**. Shipping US1 alone already gives the spec's outcome: unaddressed group
   messages never draft, including in former `always` chats (SC-001–SC-004). The column
   lingers unused and the old buttons still write it, with no effect.
2. **US2** removes the dead setting, adds the stale-button note, the texts and the migration
   (FR-004, FR-006, FR-007, SC-005). Ship both in one PR; US1 → US2 is the commit order.
3. Polish keeps the 001 documents consistent (FR-010) and validates on a live test account.
