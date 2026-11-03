# Control Chat Contract: Search Over Earlier Conversations

**Feature**: [../spec.md](../spec.md)

Changes to the control-bot UI from `specs/001-telegram-reply-agent/contracts/control-chat.md`.
There is **no search command** (clarification 2026-10-22). Texts in `src/scribe/i18n/en.py` and
`ru.py`; English shown, Russian in brackets.

## Draft card

Source labels for earlier messages (inline "Sources" line, `render_draft_card`):

```text
💬 <chat title> · <d MMM yyyy>
```

New system warnings (`card.warning.*`):

| Code | EN | RU |
|---|---|---|
| `earlier_search_unavailable` | ⚠️ drafted without searching earlier conversations | ⚠️ черновик без поиска по прошлой переписке |
| `earlier_search_no_match` | 🔎 nothing found in earlier conversations | 🔎 в прошлой переписке ничего не нашлось |
| `earlier_search_off` | 🔎 earlier conversations not searched (turned off in /settings) | 🔎 поиск по прошлой переписке выключен (/settings) |

## "Sources" view (`sources:<draft_id>` callback)

Each cited earlier message is one line:

```text
• 💬 <chat title> · <sender> · <d MMM yyyy HH:mm> — <used_for>
  «<excerpt ≤ 200 chars>»
  <https://t.me/c/<internal id>/<message id>>   # supergroups only
```

For drafts stored before this feature, or after the excerpt was purged, the line falls back to
`💬 earlier message` [`💬 сообщение из прошлой переписки`].

## Settings (`/settings`)

New row and toggle button (texts in `src/scribe/i18n/en.py` / `ru.py`):

| Key | EN | RU |
|---|---|---|
| `settings.search_earlier` | 🔎 Search earlier conversations: {state} | 🔎 Поиск по прошлой переписке: {state} |
| `settings.search_earlier_state.on` | on | вкл |
| `settings.search_earlier_state.off` | off | выкл |
| `btn.search_earlier_off` (shown while on) | 🔎 Search earlier: turn off | 🔎 Выключить поиск |
| `btn.search_earlier_on` (shown while off) | 🔎 Search earlier: turn on | 🔎 Включить поиск |

The button is the last row of the `/settings` keyboard. Callback data
`settings:search_earlier` (`format_callback("settings", "search_earlier")`, constant
`SEARCH_EARLIER` in `control_bot/settings.py`): each press flips
`users.search_earlier_messages`, answers the callback with `alert.saved` and edits the
`/settings` message in place (or sends it again when the edit fails); logs `settings_changed`
with `fields=["search_earlier_messages"]`.

## `/my_data`

No new category: cited excerpts are part of drafts and counted there. The export includes them
decrypted inside each draft as `output_json.earlier_messages` (`ref`, `chat_id`, `message_id`,
`sent_at`, `chat_title`, `sender`, `excerpt`); `/delete_my_data` deletes them with the drafts.
