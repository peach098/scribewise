# Research: Work Account Mode (008)

Phase 0 decisions for [plan.md](plan.md). The spec has no open clarification markers; the
questions below are design choices made against the current code.

## R1. Where the "is this a work chat?" decision lives: materialised `enabled` flag

**Decision**: keep `work_chats.enabled` as the single *effective* flag every reader uses, and
maintain it for chats picked up by the mode. Two new columns record why a chat is a work chat:
`work_chats.origin` (`picked` | `auto`) and `work_chats.excluded` (the user's own "off" for an
`auto` chat). For `auto` chats the invariant is
`enabled = users.work_account AND NOT excluded`; it is kept by the three writers that can
change it (intake pick-up, mode switch, exclude/include). `picked` chats are unchanged: their
`enabled` is the user's choice, and `excluded` stays `false`.

**Rationale**: today at least ten code paths filter on `WorkChat.enabled` or
`chat.enabled`: intake (`intake/router.py`), manual-reply handling (`intake/events.py`, twice),
the composer's `FOR SHARE` check before storing a draft (`drafting/composer.py`), earlier-
conversation search scope (`context/earlier.py`, three queries), style import
(`style/importer.py`), profile build and weekly refresh (`style/profiles.py`, two), and
`/skipped` (`control_bot/skipped.py`). Materialising the flag means none of them change, so a
picked-up chat behaves exactly like a picked one (spec FR-012) by construction, and the
existing disable protocol (lock chat row, then pending drafts, then turns;
`settings.disable_chat`) keeps protecting the composer race.

**Alternatives considered**:
- *Computed predicate* `enabled AND (origin = 'picked' OR users.work_account)` at every reader:
  no denormalisation, but every one of the ten queries changes (a join to `users` or an extra
  argument), each is a chance to miss a site and leak/ignore messages, and the composer lock
  protocol would need the user row too. Rejected: higher risk for the same behaviour.
- *Creating a row for every private dialog when the mode is turned on*: simplest reads, but it
  stores titles and ids of hundreds of chats that may never write, and `/chats` would explode.
  Rejected on data minimisation (Principle II) and UX.

## R2. When a chat is picked up: lazily, on the first new message in either direction

**Decision**: a chat becomes an `auto` work chat when (a) a new incoming or outgoing message
arrives in an eligible private chat with no `work_chats` row while the user's mode is on; (b)
the user excludes a chat from the exclusion picker before it ever wrote (row created
`enabled = false, excluded = true`); or (c) the style import with the mode on reads the chat
and finds the user's own messages in it (row created enabled, so the contact can get a
profile). Nothing else creates rows.

**Rationale**: spec FR-004 / edge case "hundreds of chats". Picking up on outgoing messages too
keeps the user's opening message as context for the contact's answer (spec US1 scenario 7) and
lets commitment extraction see it, like in any work chat.

**Alternatives considered**: incoming only (loses the opening message; drafts for "you asked X"
answers lack context) — rejected.

## R3. Eligibility: decided at the gateway from the Telegram entity

**Decision**: the gateway reports a `peer_kind` for private chats on both `IncomingMessage` and
`OutgoingEvent`: `user` (eligible), `bot`, `self`, `service`, `deleted`; `None` for groups.
`service` is Telegram's notifications account (id 777000) and Telegram's support accounts
(Telethon `User.support`). Only `chat_kind == "private" and peer_kind == "user"` is eligible
(`scribe/chats/work_account.py: is_eligible`). `dialog_kind()` in `user_client.py` additionally
leaves deleted accounts and the service account out of `list_dialogs`, so neither the setup
picker nor the exclusion picker offers them.

**Rationale**: Telethon already resolves the chat entity (`event.get_chat()` / `get_sender()`
is cached in its entity cache), so the flags cost no extra request in the common case.
Keeping the decision in one pure function makes it unit-testable with the fake gateway.
The Scribe control bot is a bot, so it can never become a work chat (no feedback loop of draft
cards being drafted).

**Alternatives considered**: deciding in intake from stored contacts (no bot flag stored) —
rejected; a static deny list of ids only — misses other bots.

## R4. Concurrency of the mode switch with intake and the composer

**Decision**:
- *Turning off*: one transaction sets `users.work_account = false` with `SELECT … FOR UPDATE`
  on the user row and commits. Then the existing per-chat disable protocol runs for every
  `auto` chat with `enabled = true`, in id order, one short transaction each
  (`deactivate_chat`, a generalisation of `_disable_chat_once` that does not set `excluded`,
  and drops with reason `work_account_off`).
- *Pick-up in intake/events*: the code that creates an `auto` row reads the user row
  `FOR SHARE` in the same transaction and creates the row only if `work_account` is true. So a
  pick-up that started before the switch commits first and its row is swept; one that starts
  after sees `false` and ignores the message.
- *Turning on*: one transaction sets the flag and runs
  `UPDATE work_chats SET enabled = true WHERE user_id = … AND origin = 'auto' AND NOT excluded`.
  Nothing is dropped.
- The composer is untouched: it already reads the chat row `FOR SHARE` before storing a draft
  and ends the turn without a draft when `enabled` is false.

**Rationale**: reuses the proven lock order (chat → drafts → turns). Per-chat transactions keep
lock time short on accounts with many picked-up chats; the flag committed first guarantees no
new chat is picked up meanwhile. If the process dies mid-sweep, the startup/maintenance step
(R8) finishes it.

## R5. Exclusion vs disabling

**Decision**: one user-facing concept per chat kind. For `picked` chats "Disable" keeps its
meaning (`enabled = false`). While the mode is on, the private chat's screen says
"🚫 Exclude" / "↩️ Include again"; for an `auto` chat this sets `excluded` (and `enabled` per
R1), for a `picked` private chat it is the same as disable. Excluding drops waiting turns and
pending drafts with reason `chat_disabled` (existing card status "chat disabled" is reused with
a new text "chat excluded" chosen by origin). The excluded count shown in `/settings` and
`/chats` = private `auto` chats with `excluded` + private `picked` chats with `enabled = false`
(spec FR-008: previously disabled private chats count as excluded).

## R6. Style import scope with the mode on

**Decision**: `import_own_messages` gets its targets from a new
`work_account.import_targets(...)`: all enabled work chats (as today) plus, when the mode is on,
the most recently active eligible private dialogs from `gateway.list_dialogs` (Telethon returns
dialogs newest first) that have no row or an enabled row, up to
`Settings.work_account_import_max_chats` (default 50). Excluded chats and unpicked groups are
never targets. After the import, every newly read dialog with at least one own message gets an
`auto` row (R2 c) before `build_profiles_from_import` runs, so it is a contact for profile
ranking (issue #29: setup builds contact profiles from the import; `own_message_count_6m`).

**Rationale**: bounds setup time and Telethon flood exposure (one `iter_messages(from_user=me)`
per chat, like today for picked chats; 50 matches typical "12 of many" accounts seen in testing). Issue
#30's per-group caps and short-message share apply unchanged; the mode adds only private chats,
which fill the private share of the global sample.

**Interaction**: with the mode on, many more contacts compete for the 20 contact-profile slots
(001 FR-034). Ranking by own-message count is the intended behaviour; pinned contacts keep
their slots. Whichever of #29/#30 lands first, this feature only changes the *target list*.

## R7. UI: picker reuse and `/chats` paging

**Decision**:
- The setup picker keyboard moves to `control_bot/chat_picker.py` (`picker_keyboard`,
  `PickerState`) with a `work_account` flag and an `excluded` set. Spec 007 (issue #32) also
  needs the picker outside setup; both features use this module, whichever lands first creates
  it and the other rebases onto it.
- New callbacks (all ≤ 64 bytes): `pickwa:<on|off>` (setup switch), `settings:work_account`,
  `wacct:<on|off|cancel>` (confirmations), `chatspage:<n>` (`/chats` paging), `wxopen:0`,
  `wxpick:<telegram_chat_id>`, `wxpage:<n>`, `wxdone:0` (exclusion picker), and the existing
  `chatenable:<work_chat_id>:<on|off>`.
- `/chats` is paged at 8 chats per page (same `PAGE_SIZE` as the picker). While the mode is
  off, `auto` chats are hidden and summarised in one line ("N chats picked up by work account
  mode are paused").

## R8. Leftover sweep

**Decision**: a small idempotent function `work_account.reconcile_user(user_id)` re-applies the
R1 invariant (enable `auto` chats that should be on, run `deactivate_chat` on those that should
be off). It runs after every mode switch and once at worker start for users whose flag changed
within the last day (`users.work_account_changed_at`). No new scheduler job.

**Rationale**: covers a crash in the middle of a turn-off sweep without a periodic job
(Principle VI).

## R9. History, logs and data export

**Decision**: new `activity_kind` values `work_account_on`, `work_account_off`, `chat_added`
(meta `{"work_chat_id", "origin": "auto"}`; spec 007 uses the same kind with
`origin: "picked"`), `chat_excluded`, `chat_included`. `/history` renders the chat title by
`work_chat_id` at display time (title is not copied into meta). Structured logs:
`work_account_switched` (user id, on/off, swept chat count, dropped draft count),
`work_chat_picked_up` (user id, work chat id, direction), `work_chat_excluded` /
`work_chat_included`, `work_account_ineligible` at debug level with `peer_kind` only. No titles,
no content. `/my_data` summary and export gain the mode, and counts/lists of picked-up and
excluded chats; deletion is covered by the existing cascade on `users`.

## R10. Migration

**Decision**: one Alembic revision (next free number at implementation time; `0009` on today's
main, but spec 006 may take it): add `work_chat_origin` enum and `work_chats.origin`
(`NOT NULL DEFAULT 'picked'`), `work_chats.excluded` (`NOT NULL DEFAULT false`), check
constraint `origin = 'auto' OR NOT excluded`, `users.work_account` (`NOT NULL DEFAULT false`),
`users.work_account_changed_at` (nullable). New `activity_kind` and `dropped_reason` values are
added first in autocommit mode, as in 0008. Existing rows become `picked`; no data rewrite.

## R11. Evaluation (Principle V)

**Decision**: no prompt, model or retrieval change, so the draft evaluation set is not a gate.
The style import's target list changes when the mode is on; the style evaluation is unaffected
for mode-off users (default). Deterministic code (eligibility, invariant, intake pick-up, switch
sweep, pickers) is covered by tests written first.
