# Control Chat Contract: Auto-Send for Trusted Chats

Extends `specs/001-telegram-reply-agent/contracts/control-chat.md` (draft card, `/settings`,
`/chats`, `/history`). Callback data follows `render.format_callback` (`action:entity[:extra]`,
≤ 64 bytes). Russian texts are final wording drafts; the i18n lint requires both catalogs to
have the same keys and placeholders.

## 1. Draft card in an auto-send chat

### Armed (eligible, countdown running)

Extra line above the buttons:

| Key | en | ru |
|---|---|---|
| `card.auto_send_at` | `⏱ Will be sent automatically at {time}` | `⏱ Отправлю автоматически в {time}` |

Keyboard (replaces `draft_keyboard` while armed):

| Button key | en | ru | Callback |
|---|---|---|---|
| `btn.send_now` | `✅ Send now` | `✅ Отправить сейчас` | `approve:<draft_id>` |
| `btn.edit` (existing) | `✏️ Edit` | `✏️ Изменить` | `edit:<draft_id>` |
| `btn.autostop` | `⏹ Don't send automatically` | `⏹ Не отправлять автоматически` | `autostop:<draft_id>` |
| existing | 🔁 / ❌ / 🧾 | | `regen:` / `reject:` / `sources:` |

### Stopped or not eligible (auto-send chat, manual approval needed)

Ordinary card and keyboard, plus one line:

| Key | en | ru |
|---|---|---|
| `card.auto_send_manual` | `✋ Needs your approval: {reasons}` | `✋ Нужно твоё решение: {reasons}` |
| `card.auto_send_stopped` | `⏹ Automatic sending cancelled` | `⏹ Автоотправка отменена` |

`{reasons}` joins `autosend.reason.<code>` labels with ", ": `hard_stop` (suspicious
message), `commitment` (new promise), `sensitive`, `placeholders` (needs details),
`dates_differ`, `conflict`, `weak_style`, `out_of_office`, `warning` (warning on the draft),
`outdated` (new messages), `possibly_unnecessary` (reply may not be needed),
`not_first_version` (you're already working on this draft), `outside_hours`, `paused`,
`send_failed`.

The time in `card.auto_send_at` is when the countdown ends counted from the moment the card
is posted; the send never happens earlier. A card that could not be posted never gets a
countdown; if it is posted later (issue #15), the countdown starts then.

### After sending

| Key | en | ru |
|---|---|---|
| `status.sent_auto` | `✅ Sent automatically at {time}` | `✅ Отправлено автоматически в {time}` |

### Callback behaviour

- `autostop:<id>`: stops the countdown (`auto_send_stop = user_action`), writes an
  `auto_cancelled` decision, re-renders as an ordinary card with `card.auto_send_stopped`,
  answers with no popup.
- `approve`, `edit`, `reject`, `regen`, `fill` on an armed card first stop the countdown, then
  behave exactly as today (including the existing confirmations).
- Any of them after the step has fired (`auto_approved_at` set) answers the popup
  `autosend.too_late`: `Already being sent automatically` / `Уже отправляется автоматически`;
  nothing else happens.

## 2. Offer message

Sent by the advisor as a standalone message in the control chat:

| Key | en | ru |
|---|---|---|
| `autosend.offer` | `In “{chat}” you sent {unchanged} of {total} drafts unchanged in the last 30 days. Send drafts in this chat automatically? You'll still see each one and can stop it within {window}.` | `В чате «{chat}» за 30 дней ты отправил без правок {unchanged} из {total} черновиков. Отправлять черновики в этом чате автоматически? Каждый ты всё равно увидишь и сможешь остановить в течение {window}.` |

Grammatical gender: the ru text uses the user's form (`отправил`/`отправила`) via the
existing gendered-text helper.

| Button key | en | ru | Callback |
|---|---|---|---|
| `btn.autosend_offer_on` | `Turn on` | `Включить` | `autooffer:<work_chat_id>:on` |
| `btn.autosend_offer_later` | `Not now` | `Не сейчас` | `autooffer:<work_chat_id>:later` |
| `btn.autosend_offer_never` | `Don't offer for this chat` | `Не предлагать для этого чата` | `autooffer:<work_chat_id>:never` |

- `on` → the confirmation in §3 with `autoconfirm:<work_chat_id>:offer`, then enabled with
  `source = offer`.
- `later` → `auto_send_offer_not_before = now + 30 d`; message edited to
  `autosend.offer_later` (`OK, I won't ask about this chat for a month.`).
- `never` → `auto_send_offer = opted_out`; edited to `autosend.offer_never`.
- Answers on a stale offer still apply; the figures are not recomputed.

## 3. Confirmation (manual enable and offer "Turn on")

| Key | en |
|---|---|
| `autosend.confirm` | `Auto-send for “{chat}”: drafts without warnings will be sent from your account {window} after they appear here, unless you stop them. Drafts with promises, sensitive topics, missing details or suspicious messages always wait for you. Turn it on?` |

Buttons: `autoconfirm:<work_chat_id>:manual` or `autoconfirm:<work_chat_id>:offer` (`Yes, turn
on`; the extra is the source: `manual` from `/chats`, `offer` from §2), and
`autoconfirm:<work_chat_id>:no` (`Cancel`). On yes: `auto_send = true`, `auto_send_source` from
the extra, activity `auto_send_on`, message edited to `autosend.enabled`. The help text adds:
deleting a card in the bot chat does not cancel it; use ⏹.

## 4. `/chats` per-chat screen

New line and button in `chat_settings_text` / `chat_settings_keyboard`:

| Key | en | ru |
|---|---|---|
| `chat.auto_send` | `⏱ Auto-send: {state} · {unchanged} of {total} sent unchanged (30 days)` | `⏱ Автоотправка: {state} · без правок {unchanged} из {total} (30 дней)` |

| State | Button | Callback |
|---|---|---|
| off | `⏱ Turn on auto-send` | `chatauto:<work_chat_id>:on` → confirmation §3 |
| on | `⏱ Turn off auto-send` | `chatauto:<work_chat_id>:off` → immediate, re-render |
| opted out of offers | `🔔 Offer auto-send again` | `chatauto:<work_chat_id>:reset` → `auto_send_offer = none` |

`{total} = 0` renders `chat.auto_send_no_data` (`⏱ Auto-send: {state} · no decisions yet`).

## 5. `/settings`

One new line and two buttons:

| Key | en |
|---|---|
| `settings.auto_send` | `⏱ Auto-send: {count} chats · cancel window {window}` (plural forms; `off` when 0) |

| Button | Callback | Behaviour |
|---|---|---|
| `⏱ Cancel window` | `settings:auto_send_window` | inline choice `autowin:<seconds>` for 60, 120, 300, 600, 900 |
| `⏹ Turn off auto-send everywhere` (only when count > 0) | `settings:auto_send_off_all` | immediate; countdowns stop; re-render; at most 2 taps from `/settings` (SC-005) |

A "list of chats with auto-send on" (FR-019) is the `/chats` list filtered by a new
`chats:auto` page, reached from the `settings.auto_send` line's button `⏱ Chats with
auto-send` (`settings:auto_send_list`), each row showing the chat's rate.

## 6. Suggestion to turn off (FR-020)

| Key | en |
|---|---|
| `autosend.warn` | `In “{chat}” you stopped or changed {interventions} of the last {sample} automatic drafts. Turn auto-send off for this chat?` |

Buttons: `chatauto:<work_chat_id>:off` (`Turn off`), `autowarn:<work_chat_id>:keep` (`Keep
it on`, only edits the message).

## 7. `/history`

New labels `history.kind.auto_send_on`, `auto_send_off`, `auto_send_offered`,
`auto_send_offer_answered`, `auto_approved` (`Approved automatically`), `auto_send_stopped`.
Automatic sends show `auto_approved` followed by `sent`.

## 8. Untrusted input

No text from an incoming message is rendered into any of these messages except the chat
title, which is already shown on cards today. Callbacks are checked by the existing
`OwnershipMiddleware` (`work_chat_id` / `draft_id` must belong to the caller).
