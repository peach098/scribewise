# Quickstart: Validate Adding Work Chats

## Automated

```
uv run pytest tests/unit/test_chat_picker.py tests/integration/test_add_chats_flow.py \
  tests/integration/test_us1_setup_flow.py tests/integration/test_connect_flow.py \
  tests/unit/test_i18n_lint.py
uv run pytest            # full suite: setup behaviour must be unchanged
```

| Test | Proves |
|---|---|
| `test_chat_picker` (unit) | Keyboard rows, paging and the Done label are the same for both purposes. The exclusion filter drops existing work chat ids, enabled and disabled. Page clamping. |
| `test_add_chats_flow` → adding | ➕ shown under the list and on an empty list, and hidden during setup. The picker lists only new dialogs. Done inserts enabled rows and edits the picker to "Added N". `/chats` follows. One `chats_added` record whose meta holds ids only (US1, FR-001–008, FR-018). |
| … → already added / double Done | A second Done or a stale snapshot creates no second row and does not re-enable a disabled chat. The count and the record cover only new rows. Done with nothing selected → "Nothing was added" (FR-007, SC-003). |
| … → leaving | `/cancel` gives "Nothing was added" and writes nothing. `/settings` and `/help` close the picker and are answered. Stale `pick*` taps and taps with no open picker get the "list closed" alert (FR-011–012). |
| … → Telegram | Session not active → the disconnected text and the reconnect keyboard. `fail_list_dialogs` → the setup "couldn't reach Telegram" text with /chats. No state and no rows in either case (FR-006, SC-005). |
| … → monitoring | After Done, a new `FakeMessagingGateway.emit_new_message` in the added chat creates a waiting turn. A group chat follows the group rule (FR-009–010, SC-002). |
| … → profile update | The own history of the new chat is imported, and a new global profile version is built with the contact profile when it is eligible, followed by the Rebuild result note. With no own messages there: "nothing to learn" and no build. A failing LLM gives the failure note while the chats stay. A held `profile_build_lock` makes the update wait, then run (FR-013–016, SC-004). |
| … → access and languages | A suspended user's `chatadd:` gets the gate answer. Russian texts (FR-017, FR-019). |
| … → `/history` | "➕ Work chats added · A, B" with at most 3 titles and "+N more" (US4, SC-006). |
| `test_us1_setup_flow`, `test_connect_flow` | The setup picker still works after the move: it requires at least one chat, then goes to the time zone step (FR-003). |

## Migration

```
uv run alembic upgrade head      # 0012_chats_added: adds activity_kind 'chats_added'
```

On the hosted instance, back up the database before
upgrading, as for every migration.

## Manual (staging bot, real Telegram account)

1. Finish setup with one work chat. Then create a new Telegram group with a second account and
   write a few messages in it from the main account.
2. Send `/chats` and check that **➕ Add work chats** appears under the list.
3. Tap ➕. The new group is listed, and the chat picked at setup is not. Tick the group and tap
   **Done**. You should see "✅ Added 1 chat…" and then the `/chats` list with the group.
4. Within a minute you should get the Rebuild result note, or "nothing to learn" if your own
   messages there are older than 6 months.
5. From the second account, tag the main account in the group. After the reading delay a
   draft card should appear. A plain message without a tag gets no draft (spec 006).
6. Tap ➕ again and send `/cancel`. The bot answers "Nothing was added". Tap a button on the old
   picker message: you get the "list closed" alert.
7. Send `/history` and check the line "➕ Work chats added · <group title>".
8. Switch the bot language (`/settings` → language → Русский) and repeat step 3 to check the
   Russian texts.
