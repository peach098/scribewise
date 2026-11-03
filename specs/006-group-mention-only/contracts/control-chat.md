# Contract: Control Chat Changes

Delta against `specs/001-telegram-reply-agent/contracts/control-chat.md` (`/chats` chat settings).

## Chat settings card (group chats)

Text line (replaces `👥 Drafts in this group: {mode}`):

| Key | en | ru |
|-----|----|----|
| `settings.chat_group` | 👥 Drafts in this group: only when you are tagged or replied to | 👥 Черновики в этой группе: только когда вас отметили или ответили на ваше сообщение |

It is shown for every non-private chat. It takes no placeholder.

Keyboard: the row **👥 Mentions only / 👥 Every message** is removed. Other rows (delay,
language, formality, skip, enable/disable) are unchanged.

## Stale callback

`chatgroup:<work_chat_id>:<mentions_only|always>` (still parsed, still ownership-checked):

- Another user's chat, or a missing chat: as today (`alert.chat_not_found`).
- Own chat: no write, no settings re-post, and `callback.answer(t("alert.option_removed"))`.

| Key | en | ru |
|-----|----|----|
| `alert.option_removed` | This option is no longer available | Эта настройка больше недоступна |

## Setup (chat picker)

`setup.pick_prompt` gains one sentence:

- en: "In groups I prepare a draft only when you are tagged or someone replies to your message."
- ru: "В группах я готовлю черновик, только когда вас отметили или ответили на ваше сообщение."

## Removed keys

`settings.group_mode.always`, `settings.group_mode.mentions_only`, `btn.group_mentions_only`,
`btn.group_always` (en and ru). `tests/unit/test_i18n_lint.py::test_used_catalog_keys_exist`
keeps the catalogs consistent.
