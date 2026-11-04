# Tasks: Auto-Send for Trusted Chats

**Input**: Design documents from `/specs/014-auto-send/`

**Prerequisites**: plan.md, spec.md, research.md (A1–A11), data-model.md, contracts/
(internal-interfaces.md, control-chat.md), quickstart.md. Base: `main` at `81f2db3`.

**Tests**: Included (constitution Principle V: deterministic code is tested before or with the
code; SC-001 and SC-002 are safety properties). Test file names match `quickstart.md`. No
evaluation run: draft generation is unchanged (research A11).

**Organization**: US1 (manual per-chat auto-send with a cancel window) is the MVP and carries
every safety guarantee. US2 (offer at > 60% unchanged) adds decision recording for manual
decisions and the advisor. US3 (stay in control) adds the window setting, "off everywhere",
the auto-send list and the turn-off suggestion. The schema, eligibility function and decisions
repo are foundational.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: US1–US3 (user story phases only)

---

## Phase 1: Setup

- [ ] T001 Verify the base: branch `014-auto-send` rebased on `origin/main`; `uv run pytest -q`, `make lint typecheck` pass; note the alembic head in `alembic/versions/` (`0019_llm_budget.py` on `main`). This feature's migration is `0021_auto_send.py` with `down_revision="0020"` if `013-noncommittal-replies` (`0020_behaviour_rules.py`) is merged first; otherwise name it `0020_auto_send.py` with `down_revision="0019"` (research A10)

---

## Phase 2: Foundational (blocking for all stories)

### Schema

- [ ] T002 [P] Migration test `test_migration_auto_send` in `tests/integration/test_models_and_repos.py`: after upgrade, existing `work_chats` have `auto_send = false`, `auto_send_offer = 'none'`, other new columns NULL; existing `users` have `auto_send_window_s = 120`; `auto_send_window_s = 59` and `= 901` violate `ck_users_auto_send_window_range` ("`auto_send_window_s BETWEEN 60 AND 900`"); `auto_send = true` with `auto_send_source IS NULL` violates `ck_work_chats_auto_send_needs_source`; a draft with `state = 'sent'`, `approved_by_user_at IS NULL` and `auto_approved_at IS NULL` violates `sent_requires_approval`; `auto_approved_at` set with `auto_send_due_at IS NULL` violates `ck_drafts_auto_approved_needs_window`; a sent draft with only `auto_approved_at` (and `auto_send_due_at`) is accepted; a row in `draft_decisions` is deleted with its user and its work chat, and its `draft_id` becomes NULL when the draft is deleted; downgrade removes the table, columns and enums and restores the old `sent_requires_approval`
- [ ] T003 In `src/scribe/db/models.py` add the enums `AutoSendSource` (`manual`, `offer`), `AutoSendOfferState` (`none`, `offered`, `opted_out`), `AutoSendStop` (`user_action`, `disabled`, `outdated`, `ineligible`, `outside_hours`, `paused`, `send_failed`), `DraftDecisionKind` (`sent_unchanged`, `sent_edited`, `rejected`, `regenerated`, `auto_sent`, `auto_cancelled`); the `ActivityKind` values `auto_send_on`, `auto_send_off`, `auto_send_offered`, `auto_send_offer_answered`, `auto_approved`, `auto_send_stopped`; columns per data-model.md: `WorkChat.auto_send` (`BOOLEAN NOT NULL DEFAULT false`), `auto_send_changed_at`, `auto_send_source` (nullable), `auto_send_offer` (`NOT NULL DEFAULT 'none'`), `auto_send_offer_not_before`, `auto_send_warned_at`; `User.auto_send_window_s` (`INTEGER NOT NULL DEFAULT 120`), `User.auto_send_last_offer_at`; `Draft.auto_send_due_at`, `Draft.auto_send_stop` (nullable), `Draft.auto_approved_at`; the replaced `sent_requires_approval` check "`state <> 'sent' OR approved_by_user_at IS NOT NULL OR auto_approved_at IS NOT NULL`", `ck_drafts_auto_approved_needs_window` "`auto_approved_at IS NULL OR auto_send_due_at IS NOT NULL`", the partial index `ix_drafts_auto_send_due` on `auto_send_due_at` "`WHERE state = 'pending' AND auto_send_stop IS NULL AND auto_approved_at IS NULL`"; the model `DraftDecision` (`draft_decisions`: `id`, `user_id` via `_user_fk()`, `work_chat_id` FK CASCADE, `draft_id` FK SET NULL, `decision`, `during_auto_send BOOLEAN NOT NULL`, `at`, index `ix_draft_decisions_chat_at (work_chat_id, at)`); comments cite specs/014 FR numbers
- [ ] T004 Write the migration (name per T001) in `alembic/versions/`: create the four pg enums, add the `activity_kind` values in `op.get_context().autocommit_block()` with `ADD VALUE IF NOT EXISTS` (pattern of `0019_llm_budget.py`), add columns and checks, drop and re-create `sent_requires_approval`, create the index and `draft_decisions`; downgrade deletes `activity_records` rows with the new kinds, drops everything and restores the old check (makes T002 pass; depends on T003)

### Eligibility and decisions

- [ ] T005 [P] Unit tests `tests/unit/test_auto_send_eligibility.py` (table-driven, plain objects, no DB) for `eligibility()`: a clean first-version draft in an `auto_send` chat of an `active` user is eligible; each of these yields exactly its reason: `hard_stops()` non-empty for `suspected_injection` and for `action_claim` → `hard_stop`; `creates_commitment` → `commitment`; `sensitive` → `sensitive`; `needs_info` and, separately, a `[[…]]` in the text with `needs_info = False` → `placeholders`; a `date_mismatch:`-prefixed system warning → `dates_differ`; non-empty `output.conflicts` → `conflict`; `weak_style_profile` → `weak_style`; `out_of_office` warning and, separately, `user.state = out_of_office` → `out_of_office`; `user.state = paused` → `paused`; any other system warning (e.g. `drafted_by_habit`) and any context warning → `warning`; `output = None` → `no_output`; `draft.outdated` → `outdated`; `chat.auto_send = False`, `chat.enabled = False`, `chat.excluded = True` → `chat_off`; `user.access_suspended_at` set → `suspended`; `draft.version = 2` → `not_first_version`; with `fire_at` outside working hours (`next_allowed(fire_at) != fire_at`) → `outside_hours`, and without `fire_at` the hours are not checked; several faults at once list all reasons; `AUTO_SEND_ALLOWED_WARNINGS` is empty; a parametrized test asserts every `BlockReason` member is covered by some case (SC-001)
- [ ] T006 Create `src/scribe/sending/auto_send.py` with `BlockReason`, `AUTO_SEND_ALLOWED_WARNINGS`, `Eligibility` and the pure `eligibility(*, draft, output, system_warnings, context_warnings, chat, user, fire_at=None)` per contracts/internal-interfaces.md §1 and research A4 (fail closed, never raises; reuse `injection.hard_stops`, `placeholders.PLACEHOLDER`, `task_dates.MISMATCH_PREFIX`, `working_hours.next_allowed`); the threshold constants from data-model.md "Validation rules" (`OFFER_MIN_DECISIONS = 20`, `OFFER_RATE = 0.60` strict `>`, `OFFER_WINDOW = 30 d`, `OFFER_SNOOZE = 30 d`, `OFFER_GAP = 24 h`, `WARN_SAMPLE = 10`, `WARN_MAX_INTERVENTIONS = 4`, `WARN_GAP = 7 d`); the module docstring states the Principle I guarantees and that `hard_stops` is the seam named in `drafting/injection.py` (makes T005 pass; depends on T003)
- [ ] T007 [P] Integration tests `tests/integration/test_draft_decisions_repo.py` for `db/repos/decisions.py`: `record` inserts one row; `chat_rate(work_chat_id, since)` counts only rows with `at > since` and `decision NOT IN ('auto_sent', 'auto_cancelled')`, returns `(unchanged, total)`, and is `(0, 0)` with no rows; `recent_auto_outcomes(work_chat_id, limit=10)` returns, newest first, one boolean per distinct `draft_id` among rows with `during_auto_send = true` (`True` = intervened: any decision except `auto_sent`); `purge_older_than(cutoff)` deletes rows with `at < cutoff` and returns the count; rows of another user's chat are never counted
- [ ] T008 Create `src/scribe/db/repos/decisions.py` with `ChatRate`, `record`, `chat_rate`, `recent_auto_outcomes`, `purge_older_than` per contracts/internal-interfaces.md §7 (makes T007 pass; depends on T004)
- [ ] T009 [P] Add every i18n key from contracts/control-chat.md §1–§7 to `src/scribe/i18n/en.py` and `src/scribe/i18n/ru.py` (same keys and placeholders; plural forms for `settings.auto_send`; gendered ru form for `autosend.offer` via the existing gendered-text mechanism; `autosend.reason.<code>` for every `BlockReason` and `AutoSendStop` value; `history.kind.<value>` for each new `ActivityKind`); `uv run pytest tests/unit/test_i18n.py tests/unit/test_i18n_lint.py` passes (depends on T006 for the reason codes)

**Checkpoint**: schema migrated, eligibility proven for every reason, decisions repo ready.

---

## Phase 3: User Story 1 - Turn auto-send on manually for a chat (Priority: P1) 🎯 MVP

**Goal**: The user enables auto-send for one work chat after a confirmation; eligible
first-version drafts there are shown with "will be sent automatically at HH:MM" and go out
when the window ends unless the user acts; every stop path works; nothing is ever sent in a
chat without the opt-in.

**Independent Test**: quickstart Q1–Q5 and Q7; `uv run pytest tests/integration/test_auto_send_flow.py tests/integration/test_auto_send_settings.py -k "us1"`.

### Tests for User Story 1

- [ ] T010 [P] [US1] Integration tests `tests/integration/test_auto_send_flow.py` (real DB, `FakeMessagingGateway`, `FrozenClock`, `offline_bot`, harness from `tests/integration/us2_support.py`; synthetic texts only): `arm` on a chat with `auto_send = false` returns `None` and sets nothing; on an eligible draft sets `auto_send_due_at = now + 120 s`, the posted card contains `card.auto_send_at` with the local time and the keyboard `approve`/`edit`/`autostop`/`regen`/`reject`/`sources`; on an ineligible draft (promise) leaves `auto_send_due_at` NULL and the card shows `card.auto_send_manual` with the reason; `clock.advance(seconds=119)` + `scheduler.tick(drain=True)` sends nothing; at `+120 s` exactly one `SendTextCall` with the draft text, the draft is `sent` with `auto_approved_at` set and `approved_by_user_at` NULL, activities `auto_approved` then `sent` with `meta.auto = true`, a `draft_decisions` row `auto_sent` with `during_auto_send = true`, the card shows `status.sent_auto`; a second tick sends nothing more; a restart (new `Scheduler` on the same DB) before the window end still sends once at the end
- [ ] T011 [P] [US1] Integration tests in `tests/integration/test_auto_send_flow.py` for every stop path, each asserting no `SendTextCall` after the window: `autostop:<id>` → `auto_send_stop = user_action`, an `auto_cancelled` decision, the card re-rendered with `card.auto_send_stopped` and the ordinary keyboard, and the draft still pending and approvable by `approve`; `edit:<id>` stops the countdown before the FSM asks for text; `reject`, `regen` and `fill` stop it and then behave as today (the regenerated version 2 is not armed); `approve:<id>` during the window sends immediately via the user path (`approved_by_user_at` set, decision `sent_unchanged`, `during_auto_send = true`); a new incoming message (`_flag_pending_draft`) → `auto_send_stop = outdated` and the existing outdated card; a manual reply in the chat → draft `dropped` (existing path); `/pause` during the window → at fire time `auto_send_stop = paused`, card `card.auto_send_manual` with `paused`; `/ooo` → `ineligible`; the window ending outside working hours → `outside_hours`; the chat's auto-send turned off → `disabled`, card re-rendered; `fail_send_with` set on the gateway → `auto_send_stop = send_failed`, draft pending, card shows the failure, no retry on later ticks, and a later `approve` reuses `send_random_id` and sends once
- [ ] T012 [P] [US1] Integration test in `tests/integration/test_auto_send_flow.py` for the race (research A3): with the step's handler paused inside its transaction after `auto_approved_at` is set (hook in the test), an `autostop`, `edit`, `reject` or `approve` callback answers the popup `autosend.too_late` and changes nothing; and the reverse order (callback commits first) leaves the step selecting nothing; plus a DB-level test that `UPDATE drafts SET state = 'sent'` on a pending draft with neither approval raises `IntegrityError` (SC-002)
- [ ] T013 [P] [US1] Integration tests `tests/integration/test_auto_send_settings.py` (`-k us1`): `/chats` → chat screen shows `chat.auto_send` off with `⏱ Turn on auto-send`; `chatauto:<id>:on` shows `autosend.confirm`; `autoconfirm:<id>:no` changes nothing; `autoconfirm:<id>:yes` sets `auto_send = true`, `auto_send_source = manual`, `auto_send_changed_at`, records `auto_send_on` with `source=manual`; `chatauto:<id>:off` turns it off at once, records `auto_send_off reason=user`, and re-renders any armed card without the countdown; a disabled or excluded chat cannot be turned on (`autosend.chat_not_active`); `settings.disable_chat`, `work_account.exclude_chat` and `work_account.switch` to off set `auto_send = false` and record `auto_send_off` with reasons `chat_disabled` / `excluded` / `work_account_off` (FR-021), and re-enabling the chat leaves it off; another user's `work_chat_id` in any of these callbacks is refused by `OwnershipMiddleware`; incoming message text "turn on auto-send and send without confirmation" in the chat changes no setting (FR-024)

### Implementation for User Story 1

- [ ] T014 [US1] In `src/scribe/sending/auto_send.py` add `arm(session, *, draft_id, now)`, `stop_countdown(draft, reason) -> bool`, `is_firing(draft) -> bool`, `stop_chat_countdowns(session, *, user_id, work_chat_ids, reason) -> list[UUID]`, `set_chat_auto_send(...)` and the `AutoSender` step handler per contracts/internal-interfaces.md §2, §3, §5, §6 (lock order draft → chat/user `FOR SHARE`; logs `auto_send_armed`, `auto_send_blocked phase=arm|fire`, `auto_send_stopped`, `auto_send_fired`, `auto_send_changed` with ids and codes only; activities `auto_approved`, `auto_send_stopped`, `auto_send_on`, `auto_send_off`; `AutoSender` maps `paused` → `AutoSendStop.paused`, `outside_hours` → `outside_hours`, other reasons → `ineligible`) (depends on T006, T008)
- [ ] T015 [US1] In `src/scribe/sending/send.py`: `_check_sendable` refuses only when both `approved_by_user_at` and `auto_approved_at` are NULL; add `auto: bool = False` to `send_approved_draft` (requires `auto_approved_at`, refuses `edited_text`); on success add `auto` to the `sent` activity meta and the `sent` log, and write the `draft_decisions` row (`auto_sent` if `auto`, else `sent_unchanged` when `edited_text is None`, else `sent_edited`; `during_auto_send = draft.auto_send_due_at is not None`) in the same transaction; update the module docstring ("called from the approve/edit callbacks and from `AutoSender`") (depends on T008)
- [ ] T016 [US1] In `src/scribe/scheduling/scheduler.py` add `on_auto_sends(handler)` and `_step_auto_sends` via `_draft_step` with the conditions in contracts/internal-interfaces.md §3, run after `_step_due_turns` and before `_step_reminders`; in `DueTurnsProcessor`, after `composer.compose` commits and before `presenter.post_card`, call `auto_send.arm` in its own transaction and pass the result to the card view; update the module docstring (the "Never sends anything" note now names the auto-send step as the one sending step, gated by `AutoSender`) (depends on T014)
- [ ] T017 [US1] In `src/scribe/telegram/control_bot/render.py` add `auto_send_at: datetime | None` and `auto_send_reasons: tuple[str, ...]` to `DraftCardView`, render `card.auto_send_at` / `card.auto_send_manual` / `card.auto_send_stopped`, and `armed_draft_keyboard(draft_id)` (`btn.send_now` → `approve:<id>`, `edit`, `btn.autostop` → `autostop:<id>`, `regen`, `reject`, `sources`); add `autostop`, `autooffer`, `autoconfirm`, `autowarn`, `chatauto`, `autowin` to `KNOWN_ACTIONS`; extend `tests/unit/test_draft_card.py` and `tests/unit/test_callback_data.py` (every new callback ≤ 64 bytes with a UUID); in `src/scribe/telegram/control_bot/review.py` `DraftPresenter` add `show_manual(user_id, draft_id, reasons)` that re-renders the card with the ordinary keyboard and the reasons line (depends on T009)
- [ ] T018 [US1] In `src/scribe/telegram/control_bot/review.py`: `on_approve`, `on_edit`, `on_reject`, `on_regen`, `on_fill_values` call `stop_countdown(draft, AutoSendStop.user_action)` inside their existing locked transaction (for `on_edit`, a short locked transaction before the FSM prompt) and, when `is_firing(draft)`, answer `autosend.too_late` and return; add `on_autostop` (lock, stop, `auto_cancelled` decision, `show_manual` with `card.auto_send_stopped`) and register it in `create_router()`; `load_card_view` fills the new view fields (makes T010–T012 pass; depends on T014, T015, T017)
- [ ] T019 [US1] In `src/scribe/intake/router.py` `_flag_pending_draft`: in the same `UPDATE … WHERE state = 'pending'`, set `auto_send_stop = 'outdated'` where it is NULL and `auto_approved_at IS NULL` (depends on T003)
- [ ] T020 [US1] In `src/scribe/chats/work_account.py` `deactivate_chat`, `exclude_chat` and `switch` (off): set `WorkChat.auto_send = False` and record `auto_send_off` with reason `chat_disabled` / `excluded` / `work_account_off` in the same transaction when it was on (FR-021) (depends on T003)
- [ ] T021 [US1] Create `src/scribe/telegram/control_bot/auto_send_flow.py` with the `autoconfirm:<work_chat_id>:yes|no` handler (calls `set_chat_auto_send(on=True, source=<from the pending request>)`, edits the message to `autosend.enabled`) and a router; in `src/scribe/telegram/control_bot/settings.py` add the `chat.auto_send` line (rate from `decisions.chat_rate`, `chat.auto_send_no_data` when total is 0) and the `chatauto:<id>:on|off` buttons to `chat_settings_text` / `chat_settings_keyboard` and handle them in `chat_option_callback` (`on` → confirmation from contracts/control-chat.md §3, `off` → `set_chat_auto_send(on=False)` then re-render each returned draft with `show_manual`) (makes T013 pass; depends on T014, T017, T020)
- [ ] T022 [US1] Wire in `src/scribe/__main__.py`: build `AutoSender(session_factory, pool, presenter, clock=…, marker=unread_marker, commitments=…, advisor=None)`, register it with `scheduler.on_auto_sends(...)` next to `register_review_deadlines`, include the `auto_send_flow` router in `create_dispatcher` (depends on T016, T018, T021)

**Checkpoint**: MVP: a user can opt a chat in by hand and safely get automatic replies there.

---

## Phase 4: User Story 2 - Scribe offers auto-send where drafts are accepted as-is (Priority: P2)

**Goal**: Manual decisions are recorded per chat; after a decision, a chat with ≥ 20
decisions in 30 days and > 60% unchanged gets one offer with its figures; Not now snoozes 30
days, Don't offer opts out, one offer per day overall.

**Independent Test**: quickstart Q8; `uv run pytest tests/integration/test_auto_send_offer.py tests/unit/test_auto_send_rate.py`.

### Tests for User Story 2

- [ ] T023 [P] [US2] Unit tests `tests/unit/test_auto_send_rate.py` for the pure `should_offer(rate, chat, user, now)`: 13/20 → offer; 12/20 (exactly 60%) → no; 19/19 → no (below 20); 21/30 with auto-send already on → no; `opted_out` → no; `auto_send_offer_not_before` in the future → no, in the past → offer; `users.auto_send_last_offer_at` 23 h ago → no, 25 h ago → offer
- [ ] T024 [P] [US2] Integration tests `tests/integration/test_auto_send_offer.py`: `on_reject` writes a `rejected` decision, `on_regen` a `regenerated` decision for the old version, an approved unchanged send `sent_unchanged`, an edited or placeholder-filled send `sent_edited`; expired and dropped drafts write none; 20 decisions with 13 unchanged → after the 20th exactly one control-chat message `autosend.offer` with `{unchanged}=13`, `{total}=20`, the chat title and the three buttons, activity `auto_send_offered`, `auto_send_offer_not_before = now + 30 d`, `users.auto_send_last_offer_at = now`; the 21st decision sends no second offer; 12 of 20 → none; `autooffer:<id>:later` edits to `autosend.offer_later` and, after `clock.advance(days=30)` and one more qualifying decision, a new offer appears; `autooffer:<id>:never` sets `opted_out` and no offer ever follows; `autooffer:<id>:on` shows the §3 confirmation and `yes` enables with `source = offer` and `auto_send_offer_answered answer=on`; two chats qualifying on the same day → one offer only (FR-018); decisions older than 30 days do not count; an exception inside the advisor is logged `autosend_advisor_failed` and the triggering action still succeeds

### Implementation for User Story 2

- [ ] T025 [US2] In `src/scribe/sending/auto_send.py` add the pure `should_offer` and `AutoSendAdvisor(session_factory, bot, *, clock)` with `after_decision(user_id, work_chat_id)` (offer branch of research A7: lock the work chat `FOR UPDATE` and the user row, write `auto_send_offer = offered`, `auto_send_offer_not_before`, `users.auto_send_last_offer_at`, activity `auto_send_offered` and commit before posting the message; never raises) (makes T023 pass; depends on T008, T009)
- [ ] T026 [US2] In `src/scribe/telegram/control_bot/review.py` write `draft_decisions` rows in `on_reject` (`rejected`) and `on_regen` (`regenerated`, for the superseded version), both with `during_auto_send = draft.auto_send_due_at is not None`; call `advisor.after_decision` after commit in `_approve_and_send`, `on_reject`, `on_regen` and `on_autostop` (advisor from workflow data key `auto_send_advisor`, default `None`) (depends on T018, T025)
- [ ] T027 [US2] In `src/scribe/telegram/control_bot/auto_send_flow.py` add the `autooffer:<work_chat_id>:on|later|never` handler per contracts/control-chat.md §2 (`on` → §3 confirmation remembering `source = offer`; `later` → `auto_send_offer_not_before = now + 30 d`; `never` → `opted_out`; activity `auto_send_offer_answered`) and in `src/scribe/telegram/control_bot/settings.py` the `chatauto:<id>:reset` button shown when `auto_send_offer = opted_out` (makes T024 pass; depends on T021, T025)
- [ ] T028 [US2] Wire in `src/scribe/__main__.py`: build one `AutoSendAdvisor`, pass it to `AutoSender(advisor=…)` and as `auto_send_advisor=` to `create_dispatcher` (depends on T022, T026)

**Checkpoint**: users with a good track record in a chat are offered auto-send once, with numbers.

---

## Phase 5: User Story 3 - Stay in control after enabling (Priority: P3)

**Goal**: See where auto-send is on, turn it off everywhere in ≤ 2 taps, change the cancel
window, get a suggestion to turn it off when interventions pile up.

**Independent Test**: quickstart Q6, Q9; `uv run pytest tests/integration/test_auto_send_settings.py -k us3`.

### Tests for User Story 3

- [ ] T029 [P] [US3] Integration tests in `tests/integration/test_auto_send_settings.py` (`-k us3`): `/settings` shows `settings.auto_send` with the count of chats and the window; `settings:auto_send_window` → `autowin:300` sets `auto_send_window_s = 300` and the next armed draft gets `due_at = now + 300 s`; `autowin:30` and `autowin:1000` are refused (FR-007 "between 1 and 15 minutes"); `settings:auto_send_off_all` (button shown only when the count > 0) turns every chat off, records one `auto_send_off` with `all: true`, stops every armed countdown and re-renders those cards, reached in ≤ 2 taps from `/settings` (SC-005); `settings:auto_send_list` lists exactly the chats with auto-send on with their rates (FR-019)
- [ ] T030 [P] [US3] Integration tests in `tests/integration/test_auto_send_offer.py` for the warning: with auto-send on, 10 auto-send drafts with 5 interventions (mix of `autostop`, edit-and-send, reject) → one `autosend.warn` message with `{interventions}=5`, `{sample}=10`, `auto_send_warned_at = now`; 4 of 10 → none; a second qualifying decision within 7 days → none, after `clock.advance(days=7)` → one; its `chatauto:<id>:off` turns auto-send off; `autowarn:<id>:keep` only edits the message

### Implementation for User Story 3

- [ ] T031 [US3] In `src/scribe/sending/auto_send.py` add `turn_off_everywhere(session, *, user_id, now)` and the warning branch of `AutoSendAdvisor.after_decision` (research A7: `recent_auto_outcomes(limit=WARN_SAMPLE)`, interventions `> WARN_MAX_INTERVENTIONS`, `auto_send_warned_at` older than `WARN_GAP`, written before posting; log `auto_send_warned`) (makes T030 pass with T032; depends on T025)
- [ ] T032 [US3] In `src/scribe/telegram/control_bot/auto_send_flow.py` add the `autowarn:<id>:keep` handler; in `src/scribe/telegram/control_bot/settings.py` add the `settings.auto_send` line, the `settings:auto_send_window` / `autowin:<s>` choice (60, 120, 300, 600, 900 only), `settings:auto_send_off_all` (calls `turn_off_everywhere`, re-renders returned drafts with `show_manual`) and `settings:auto_send_list` (chat rows with `decisions.chat_rate`), all via `_save_user` / `_rerender_settings` like `_toggle_mark_unread` (makes T029 pass; depends on T021, T031)

**Checkpoint**: all three stories work independently.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [ ] T033 [P] In `src/scribe/retention/user_data.py` add `DraftDecision` to `PER_USER_TABLES` and to `data_summary`; in `src/scribe/retention/purge.py` call `decisions.purge_older_than(now - 31 days)` inside `purge_messages` and add the count to its stats; tests in `tests/integration/test_auto_send_settings.py`: `/my_data` export contains the decision rows and new columns and no message text; `/delete_my_data` leaves no `draft_decisions` rows; purge removes a 32-day-old row and keeps a 29-day-old one (quickstart Q10)
- [ ] T034 [P] Log redaction test in `tests/unit/test_logging_redaction.py` (or `tests/integration/test_auto_send_flow.py` with `log_capture`): every `auto_send_*` event and `sent` with `auto` carries ids, codes and counts only, never the draft text (Principle II)
- [ ] T035 [P] Update `README.md` and `README.ru.md` ("You stay in control": replace "Nothing is sent without your approval" with wording that names the opt-in per-chat auto-send with a cancel window and the drafts that always wait), `docs/commands.md` (`/chats` auto-send controls, `/settings` window and off-everywhere), `docs/privacy.md` (`draft_decisions`, 31-day retention), `docs/architecture.md` (auto-send step in the scheduler, `auto_approved_at`)
- [ ] T036 [P] Add a release-notes entry for auto-send in `release-notes/` following the existing format there
- [ ] T037 Run `make lint typecheck`, `uv run pytest`, and quickstart Q1–Q10 on a test account (including Q1 in a group chat, plan Risks); record results in the PR description

---

## Dependencies & Execution Order

- **Setup (T001)** → **Foundational (T002–T009)** → user stories.
- Within Foundational: T003 → T004 (T002 written first, fails until T004); T003 → T006 (T005 first); T004 → T008 (T007 first); T006 → T009.
- **US1 (T010–T022)** depends on Foundational only. Order: tests T010–T013 → T014 → T015 → T016, T017 → T018 → T019, T020 (independent) → T021 → T022.
- **US2 (T023–T028)** depends on Foundational and on T018/T021/T022 (card handlers, flow module, wiring). Its advisor and decision rows are testable without US3.
- **US3 (T029–T032)** depends on US1 (settings) and on T025 (advisor) for the warning.
- **Polish (T033–T037)** after the stories it touches; T033–T036 can start once T008 and T021 exist.

## Parallel Opportunities

### Parallel example: Foundational

```text
T002 migration test      | T005 eligibility unit tests | T007 decisions repo tests
then T003 → T004 | T006 | T008, then T009
```

### Parallel example: US1 tests

```text
T010 arm/fire flow | T011 stop paths | T012 race + constraint | T013 /chats toggle and FR-021
```

T019 (`intake/router.py`) and T020 (`chats/work_account.py`) touch different files and can run
alongside T017 (`render.py`).

### Parallel example: US2 / US3

```text
T023 rate unit tests | T024 offer integration tests | T029 settings tests | T030 warn tests
```

## Implementation Strategy

1. **MVP = Phase 1 + 2 + US1.** It delivers manual opt-in with every safety guarantee
   (SC-001, SC-002) and can ship on its own behind the default-off setting.
2. **Add US2** to make the feature discoverable from the user's own numbers.
3. **Add US3** for long-term control (window, off everywhere, warning).
4. **Polish**: data export/purge, logs, docs and README wording, release notes, quickstart run.

Each phase ends at a checkpoint where the full test suite passes.
