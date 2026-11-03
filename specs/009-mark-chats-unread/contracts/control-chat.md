# Control Chat Contract: Mark Chats Unread After Agent Activity

Extends `specs/001-telegram-reply-agent/contracts/control-chat.md` (`/settings`) and follows
the toggle pattern of `specs/004-conversation-search/contracts/control-chat.md`
(`settings:search_earlier`).

## `/settings` message

One new line after "🔎 Search earlier conversations", before the per-chat hint:

| Key | en | ru |
|---|---|---|
| `settings.mark_unread` | `🔵 Mark chats unread after the agent: {state}` | `🔵 Отмечать чаты непрочитанными после агента: {state}` |
| `settings.mark_unread_state.on` | `on` | `вкл` |
| `settings.mark_unread_state.off` | `off` | `выкл` |

## Keyboard

One new row after the search-earlier button:

| State | Button key | en | ru | Callback |
|---|---|---|---|---|
| on | `btn.mark_unread_off` | `🔵 Unread marks: turn off` | `🔵 Не отмечать непрочитанными` | `settings:mark_unread` |
| off | `btn.mark_unread_on` | `🔵 Unread marks: turn on` | `🔵 Отмечать непрочитанными` | `settings:mark_unread` |

## Behavior of `settings:mark_unread`

- Each press flips `users.mark_unread` for the caller (ownership middleware, scoped load),
  re-renders the `/settings` message text and keyboard in place, and answers the callback with
  no popup text.
- No confirmation, no other message. No chat is marked or unmarked as a result of the press.
- A press from a stale `/settings` message works the same (it flips the current stored value).

## Nothing else changes

- No new command. No change to draft cards, `/chats`, `/history` or setup.
- Failures to mark a chat are never shown to the user (spec FR-004).
- `/my_data` lists `mark_unread` with the other `users` fields (export includes every column).
