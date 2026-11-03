# Quickstart: Validate Work Account Mode (008)

## Automated

```
uv run pytest tests/unit/test_work_account_eligibility.py tests/unit/test_user_client.py \
  tests/unit/test_chat_picker.py tests/unit/test_router.py \
  tests/integration/test_work_account_intake.py tests/integration/test_work_account_switch.py \
  tests/integration/test_work_account_bot.py tests/integration/test_work_account_import.py
uv run pytest            # full suite: the mode is off by default, existing tests stay green
```

| Test | Proves |
|---|---|
| `test_work_account_eligibility` | `is_eligible`: only private + `user`; bots, self, service, deleted, groups, missing `peer_kind` are not (FR-003) |
| `test_user_client` (extended) | `peer_kind` mapping from Telethon entities for incoming and outgoing; `dialog_kind` drops deleted/service accounts |
| `test_chat_picker` | Picker keyboard with the mode on/off: private ✅/🚫, groups ✅/⬜, Done label, paging (FR-013) |
| `test_work_account_intake` | Mode on: unknown private contact → `auto` row + turn; first-time contact; outgoing-first then answer has context; bot/self/service/group ignored, nothing stored; excluded ignored; duplicate/concurrent first messages → one row; mode off → ignored (US1, US2, FR-002–FR-005, SC-002, SC-003) |
| `test_work_account_switch` | Turn on re-enables only non-excluded `auto` chats; turn off pauses `auto` chats, drops waiting turns and pending drafts (`work_account_off`), keeps `picked` chats; on → off → on restores exclusions; concurrent pick-up during turn-off is swept; composer refuses to store a draft in a paused chat; `reconcile_user` fixes a half-done sweep (FR-008, FR-009, SC-004, SC-006) |
| `test_work_account_bot` | Setup: switch, exclude, Done with nothing ticked, confirmation. `/settings`: state line, confirmations (counts), stale `wacct:*`. `/chats`: header, marks, paging, mode-off summary, exclusion picker incl. never-written chats, Telegram unavailable, `/cancel`; chat screen Exclude / Include again; `/history` entries; en + ru (US1–US4, FR-006, FR-013–FR-015, FR-017, FR-020) |
| `test_work_account_import` | Import targets with the mode on: enabled chats + ≤ N most recent eligible private dialogs, never excluded or unpicked groups; imported chats with own messages materialised as `auto` and ranked for contact profiles; mode off unchanged (US5, FR-016) |

The i18n lint (`tests/unit/test_i18n_lint.py`) covers the new keys; the retention tests cover
the `/my_data` summary/export fields and deletion (FR-018).

## Manual (staging or a local instance with a test Telegram account)

Prerequisites: a test Telegram account A connected to Scribe, two other accounts B (has
chatted with A before) and C (never chatted with A), and a bot in A's dialog list.

1. **Migrate**: back up the DB, `alembic upgrade head`, restart the app.
2. **Setup with the mode on**: `/start` on a fresh user, connect A. In the picker press
   `💼 Work account`, exclude B, press Done without ticking anything.
   - Expected: confirmation "Work account mode is on … Excluded: 1"; setup continues to the time
     zone and import steps.
3. **First-time contact**: C writes to A. Expected: a draft card after the reading delay;
   `/chats` lists C with `· 💼`; `/history` shows "New work chat (work account) · C".
4. **Excluded**: B writes to A. Expected: no draft; `/my_data` message count unchanged.
5. **Bots and groups**: the bot sends A a message; someone posts in an unpicked group.
   Expected: nothing.
6. **Include again**: `/chats` → `🚫 Exclude chats…` → tap B → Done. B writes again. Expected:
   a draft.
7. **Turn off**: with a pending draft from C, `/settings` → `💼 Work account: turn off`.
   Expected: confirmation states 1 pending draft; after confirming, the card shows "Work
   account mode off — dropped"; C writes again → no draft; a hand-picked chat still drafts.
8. **Turn on again**: `/settings` → turn on → confirm. Expected: C drafts again; exclusions
   from step 6 are as left.
9. **Rebuild**: `/profile` → 🔄 Rebuild with the mode on. Expected: "learned from N messages"
   includes messages from A's recent private chats; the excluded chat contributes nothing
   (check `style_import_finished` log: `auto_targets`).
10. **Logs**: `docker compose logs app | grep work_account` shows ids and counts only, never
    titles or text.
