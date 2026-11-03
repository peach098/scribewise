# Research: Add Work Chats After Setup

All Technical Context items were known; no NEEDS CLARIFICATION remained. The decisions below
are grounded in the current code on `main` (2026-10-23).

## R1. One picker for setup and /chats

**Decision**: Move `picker_keyboard`, `show_chat_picker`, `pick_toggle`, `pick_page` and the
generic part of `pick_done` from `setup_flow.py` to a new `control_bot/chat_picker.py`.

- The FSM data carries `purpose` (`"setup"` | `"add"`), `dialogs`, `selected`, `page` and
  `message_id`.
- The toggle, page and done handlers are registered twice, once per FSM state:
  `SetupStates.pick_chats` in the setup router and `AddChatsStates.picking` in the add router.
  Both registrations use the same callback prefixes `pickchat:` / `pickpage:` / `pickdone:`.
- `pick_done` validates the selection. It then calls the purpose's finisher:
  - `setup_flow.finish_pick` keeps the setup behaviour: at least one chat, upsert, "Work chats
    saved: N", then the time zone step.
  - `add_chats.finish_add` is the new add behaviour.
- `show_chat_picker(..., purpose, exclude_telegram_ids)` takes the prompt key and the excluded
  ids from the purpose.

**Rationale**: the issue requires the same code ("no copy"). The state split keeps today's
routing intact:
- setup's `/cancel` text and its `StateFilter(SetupStates, ConnectStates)` stay as they are;
- the settings `idle` filter excludes setup states but can include `AddChatsStates`.

**Alternatives considered**:
- Reusing `SetupStates.pick_chats` for both purposes. Rejected: setup-only handlers (`/cancel`
  → "setup cancelled", and the commands answered by setup while it runs) would act on the add
  picker.
- A copy of the picker in `settings.py`. Rejected by the acceptance criterion.

## R2. Which dialogs the add picker lists

**Decision**: list `gateway.list_dialogs(user_id)` minus every `telegram_chat_id` in
`chats_repo.list_work_chats(session, user_id)` (enabled **and** disabled). Order and paging are
the same as in setup. The prompt has one extra line saying that chats already in `/chats` are
not listed and that a disabled one is turned back on from its settings. If nothing is left, the
bot answers `chats.add_none_left` and does not open a picker.

**Rationale**: spec FR-004/FR-005 and the issue ("a chat that was disabled earlier is not
offered as new").

**Alternatives considered**: showing the existing chats as "✔️ already added" rows that cannot
be tapped. Rejected: on accounts with many chats it wastes picker pages. It also needs a third
button state in a keyboard shared with setup, where it never applies.

## R3. Updating the profiles after adding

**Decision**: automatic, in the Done handler after the confirmation and the `/chats` list are
sent, with no extra tap.

1. Extract the body of `profile_command.rebuild_callback` (import → `build_profiles_from_import`
   with `interactive=True` → result note, failure note, abandoned-on-deletion check) into
   `rebuild_profiles(bot, user, ..., wait: bool, only_if_new_in: set[int] | None)`. The 🔄
   Rebuild button calls it with `wait=False`: a held lock still answers "Already rebuilding".
   The add flow calls it with `wait=True`: it awaits `profile_build_lock(user.id)`, so the
   update runs after a rebuild already in progress (FR-015).
2. After `import_own_messages`, if `only_if_new_in` is given and
   `sum(per_chat_counts.get(id, 0) for id in new_ids) == 0`, no profile is built. The user gets
   `chats.add_nothing_to_learn` (FR-014).
3. Otherwise the user gets the same result note as Rebuild (`profile.rebuilt`). After PR #31 it
   is `rebuild_result_text`, which also covers partial failures. On failure the user gets
   `chats.add_learn_failed`, which points to 🔄 Rebuild. The added chats stay.

The handler itself is the background job:
- aiogram's `start_polling` runs every update as its own task (`handle_as_tasks=True`, the
  default, used by `__main__.py`), so other commands are answered while the update runs.
- The FSM state is cleared before the import starts, so other commands are not blocked by the
  picker either.
- Tests drive the dispatcher with `feed_update`, which awaits the handler, so they are
  deterministic.

**Rationale**:
- The issue offered two options: a background rebuild with a note when it is done, or asking
  "Rebuild profiles now?".
- The automatic option needs no decision from the user. Drafts in the new chats are only as
  good as the profile, and a question would usually be skipped.
- Using the Rebuild path means one place builds profiles. Issue #30 (retry, sample caps) and
  any fix for #29 apply automatically.
- The full re-import is needed because the global profile is built from the newest own
  messages across *all* chats (`select_global_samples` after PR #31). The contact ranking
  (`own_message_count_6m`) is also set from a full import.
- The "nothing to learn" skip avoids an LLM build (cost and random drift) when the new chats
  hold no own messages.

**Alternatives considered**:
- Offering "Rebuild now?": one more tap, and often skipped.
- An incremental import of only the new chats plus contact profiles only: the global profile
  and the ranking would not include them, which breaks FR-013.
- A new scheduler job: more moving parts than an awaited handler (Principle VI).
- `asyncio.create_task` from the handler with a tracked task set, like `CommitmentExtractor`:
  only needed if the handler must return early. aiogram already isolates it.

**Consequences**:
- A rebuild ends the latest weekly refresh's ↩️ Undo, exactly as 🔄 Rebuild does (FR-016). The
  contract documents this.
- On a bot restart during the update, the update is lost. The chats stay added, and the user can
  press 🔄 Rebuild. The weekly refresh also picks up new messages. This is acceptable for a
  best-effort step.

## R4. Adding without duplicates and without touching existing rows

**Decision**: a new repo function `chats_repo.add_work_chats(session, user_id, chats) ->
list[WorkChat]`. It runs `INSERT … ON CONFLICT (user_id, telegram_chat_id) DO NOTHING
RETURNING …` and returns only the rows it created, with `enabled = true` and the model
defaults. The add flow uses it, while setup keeps `upsert_work_chat`. The confirmation count,
the activity record and the "new chat ids" for R3 all come from the returned rows.

**Rationale**:
- The unique constraint already exists (`work_chats` `UniqueConstraint("user_id",
  "telegram_chat_id")`).
- `upsert_work_chat(..., enabled=True)` would **re-enable a disabled chat** when the picker
  snapshot is stale or Done is tapped twice. The spec forbids that: a disabled chat is
  re-enabled only from its settings.
- `ON CONFLICT DO NOTHING` is race-safe without catching `IntegrityError`.

**Alternatives considered**: select-then-insert in a transaction. Rejected: two concurrent Done
taps can both see "absent" and one of them then fails on the constraint.

## R5. Leaving the picker: /cancel and other commands

**Decision**:
- `/cancel` in `AddChatsStates.picking` clears the state, removes the picker's buttons (best
  effort) and answers `chats.add_cancelled` ("Nothing was added").
- Any other command closes the picker first and is then handled as usual. This is
  `AbandonedPickerMiddleware` in `chat_picker.py`, built the same way as `AbandonedRulesMiddleware`
  and registered as a **dispatcher-level** message outer middleware in `bot.py`, after the access
  gate. Every command reaches it, including `/help` and `/start`, which are answered before the
  settings router runs.
- `AddChatsStates` joins the settings `idle` filter, so `/settings`, `/chats` and the other
  commands answer while the picker is open.
- Free text while the picker is open gets a one-line hint ("Tap chats to select them, then
  Done; /cancel to stop").

**Rationale**: the issue says "The picker can be cancelled with /cancel. Other commands answer
as usual once the picker is closed".

**Alternatives considered**: a router-level outer middleware on the settings router, as the
existing ones are. Rejected: `/help` is handled by the base router's own observer before the
nested settings router runs, so the picker would stay open after `/help`.

## R6. Stale picker buttons

**Decision**:
- The FSM data stores the picker's `message_id`. A `pick*` callback from a different message
  answers `alert.picker_closed` ("This list is closed. Open /chats again.") and changes nothing.
- A catch-all `pick*` handler with no state filter, registered **last** in the setup router,
  gives the same answer when no picker is open. It is registered after the state-bound ones in
  both routers. The settings router is included before the setup router.

**Rationale**: FR-012, and the spec's "Two pickers" edge case. Today a stale setup picker
callback is simply unanswered, and the button keeps spinning.

## R7. Telegram not connected or unavailable

**Decision**: before listing dialogs, `add_chats` reads `users_repo.get_telegram_session`.
- If there is no row or its status is not `active`, it answers `telegram.disconnected_start`
  with `reconnect_keyboard`. This is the text and keyboard `/start` uses for a set-up user, so
  setup and `/chats` say the same thing.
- If `list_dialogs` raises (`GatewayError`, `GatewayNotStarted`, `SessionRevoked`, a timeout
  or anything else), it answers `common.send_to_continue` with `problem =
  connect.telegram_unavailable` and `command = /chats`. This is the setup text with the command
  that retries this flow.

No state is set and nothing is written in either case. `FakeMessagingGateway` gets
`fail_list_dialogs: Exception | None` for the tests.

## R8. History record and rendering

**Decision**:
- A new `ActivityKind.chats_added` with `meta_json = {"work_chat_ids": [<uuid>, …]}`. It holds
  ids only, in line with the model's "Metadata only (ids, counts, flags)" rule. One record is
  written per Done that added at least one chat, in the same transaction as the inserts.
- `/history` renders it as `➕ Work chats added · <title>, <title>, +N more` (at most 3 titles).
- The titles are loaded in one extra query over `work_chats` scoped to the user, for the
  `chats_added` records on the page.
- A chat that no longer exists (data deletion) is left out.

**Rationale**: FR-018 ("chat titles are fine, no message content"). Copying titles into the
record would duplicate personal data, and the title would go stale after a rename.

**Migration**: `0012_chats_added`: `ALTER TYPE activity_kind ADD VALUE IF NOT EXISTS
'chats_added'` in `op.get_context().autocommit_block()`, the same pattern as 0008. No other
change. The downgrade is a no-op, because Postgres cannot drop an enum value.
- **Risk**: parallel branches add 0009–0011 and 0013, all revising 0008; this one is 0012. 006 and PR #31 add no migration as of
  2026-10-23. Renumber at merge time if main has moved.

## R9. Who sees the button

**Decision**: the ➕ button is shown only when `user.state != setting_up`. A `chatadd:` tap
from a user still in setup answers `common.not_set_up`.
- Paused and out-of-office users can add chats.
- The access gate (005) runs before every handler as a dispatcher outer middleware, so no
  extra check is needed. A suspended user only reaches `/my_data` and `/delete_my_data`.

## R10. Interactions with parallel work

- **006 (group mention only)**: added groups get the model default `group_mode =
  mentions_only`, and 006 removes the per-chat choice. The add flow asks nothing about groups,
  so it works before and after 006 merges.
- **#29 (setup builds contact profiles)**: independent. The add flow already uses
  `build_profiles_from_import`, which builds contact profiles. If #29 also moves setup to that
  function, both paths use the same builder.
- **#30 / PR #31 (sampling caps, retry)**:
  - The add flow inherits the caps and the retry through `build_profiles_from_import`.
  - If #31 merges first, the extracted `rebuild_profiles` must keep its `rebuild_result_text`
    note and the `global_profile is None` case.
  - This is a likely textual merge conflict in `profile_command.rebuild_callback`. The task
    list puts that extraction in its own task so it is easy to redo.
- **#15 / spec 008 (Work account)**: when that mode is on, private chats are work chats
  without a row per chat, or with rows created on first message; 008 decides. Possible
  outcomes for the picker: it lists only groups in that mode, or 008 replaces the exclusion
  set. `chat_picker` takes the exclusion set as a parameter, so 008 can change it without
  touching the picker.
