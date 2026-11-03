# Research: Group Chats — Draft Only When Addressed

All Technical Context items were known; the open questions were about Telegram's signals and
the migration shape.

## R1. Where does `mentions_me` come from, and what does it mean?

**Finding**: `user_client.message_to_incoming` sets
`mentions_me=bool(getattr(message, "mentioned", False))`. Telethon (installed 1.45.0) does not
compute this: `telethon.tl.custom.Message.mentioned` is the raw MTProto `message.mentioned`
flag, which the server sets on the receiving user's copy of the message. Telegram's docs
(core.telegram.org/api/mentions) say: "Incoming messages mentioning the current user will have
the `mentioned` flag set, and will contain one or more `messageEntityMention` and
`messageEntityMentionName` constructors."

Tag representations:
- `MessageEntityMention`: an `@username` span in the text. The server generates it for
  `@username` itself; clients need not send it. The server resolves it against the user's
  *current* username, so a similar or old username does not set the flag.
- `MessageEntityMentionName(user_id)`: a name-link tag (picked from the member list, or for a
  user without a username). The sender's client sends it as
  `InputMessageEntityMentionName(user_id=InputUser)`; the receiver sees `MessageEntityMentionName`.
- Plain text "Anna" carries no entity and no flag.

**Decision**: Keep the server flag as the tag signal (FR-002). Do not re-derive tags from
entities: the server flag already covers both entity types and username changes, and parsing
entities would require knowing the user's current id and username at intake for no gain.

**Alternatives considered**: Match `MessageEntityMentionName.user_id == me_id` and
`@username` entities against `users.telegram_username` locally. That is what
`found_messages` does for earlier-search results (`mentioned_peer_ids`,
`mentioned_usernames`), where no `mentioned` flag is available for other users' messages.
Rejected for intake: it duplicates the server, and it breaks when the username changes and the
stored copy is stale.

## R2. Is `message.mentioned` already true for replies to the user's messages?

**Finding**: Not documented. The mentions page speaks only of mentions; the unread-mentions
counter behaviour of official clients suggests replies are counted too, but there is no
contract. Our code does not depend on the answer: intake already computes `is_reply_to_me`
separately (`event.get_reply_message()` and `reply.out`) and ORs the two.

**Decision**: `mentions_me` needs no semantic change for replies. Keep the explicit reply
check, and when logging the reason (FR-011) check `is_reply_to_me` first: a reply is logged as
`reply` whether or not the server also set `mentioned`; `tagged` means "flag set, not a reply
to the user". The reason is for observability only and never changes the action.

**Alternatives considered**: Drop the `get_reply_message` call and rely on `mentioned` only.
Rejected: undocumented, and it saves one cached API call per group reply only.

## R3. Edge cases the current signals get wrong

1. **Forwarded messages** (spec edge case: a tag inside a forwarded message is not counted).
   A forwarded copy keeps its entities. Whether the server sets `mentioned` on it is not
   documented. **Decision**: `mentions_me = mentioned and message.fwd_from is None`.
2. **Forum topics.** A message posted in a topic without replying has
   `reply_to = MessageReplyHeader(forum_topic=True, reply_to_msg_id=<topic id>,
   reply_to_top_id=None)`. `get_reply_message()` then returns the topic's service message
   (`MessageActionTopicCreate`). That message is `out=True` when the user created the topic,
   so today every post in such a topic counts as "reply to me". This did not matter much while
   `always` existed; with one rule it would create drafts for unaddressed messages (SC-002).
   **Decision**: the pure helper `replies_to_message(message)` treats a header with
   `forum_topic=True` and `reply_to_top_id is None` as "not a reply". A header with
   `reply_to_top_id` set is a real reply inside a topic. `is_reply_to_me` also requires the
   fetched message to be an ordinary message (`action is None`) sent by the user (`out`).
3. **Cross-chat replies / quotes from another chat.** A header with `reply_to_peer_id` set to a
   different peer points outside this group. **Decision**: not a reply to the user's message
   in this group (spec Assumption).
4. **Replies to someone else's message that quote the user**: `reply.out` is false. Already
   correct.

Residual risk: if the server sets `mentioned` on plain forum-topic posts, guard 2 does not
help (it only fixes `is_reply_to_me`). Quickstart scenario 6 checks this on a live account; if
it happens, the fix is to ignore `mentioned` for headers that guard 2 rejects, provided the
text has no mention entity.

## R4. Migration: update `always` rows, or drop the column?

**Finding**: Alembic head on `main` is `0008` (`0008_skip_closing_messages.py`). `work_chats.group_mode`
is a native Postgres enum `group_mode ('mentions_only', 'always')`, NOT NULL, created in
`0001_initial.py`. Readers: `ChatRule.of` (intake), `chat_settings_text`, `chat_option_callback`,
and tests. Nothing else reads it: data export and account deletion operate on whole rows.

**Decision**: Migration `0009_drop_group_mode`: drop the column, then the enum type. No
`UPDATE ... SET 'always' → 'mentions_only'` step is needed: once the column is gone, every
group follows the single rule (FR-005), and there is no state left to drift. Downgrade
recreates the enum and the column NOT NULL with a temporary server default `'mentions_only'`
(then drops the default, matching 0001). Downgrade cannot restore which chats were `always`;
that is documented and accepted, since the mode is being retired.

**Alternatives considered**:
- Keep the column, run `UPDATE ... WHERE group_mode = 'always'`, and ignore it in code.
  Rejected: a dead setting invites reuse, keeps `GroupMode` in the model, and leaves
  `alembic check` and tests carrying a value that means nothing (Principle VI).
- Keep the column but shrink the enum to `mentions_only`. Rejected: Postgres cannot drop an
  enum value without recreating the type; it is the same work as dropping the column, with no
  benefit.

**Deploy order**: migrations are run by hand (`docker compose run --rm app alembic upgrade
head`, README "Deploy"). Old code reading `group_mode` fails against the new schema, so the
order is: stop the app, upgrade, start the new image (the usual procedure, see
docs/access-gate.md). The release note must say so.

## R5. Stale `chatgroup` buttons

**Finding**: `chatgroup:<work_chat_id>:<mentions_only|always>` is registered in
`settings.py` (`chat_option_callback`) and listed in `ownership.py` for the ownership check.
Old settings cards in the control chat keep their keyboards indefinitely.

**Decision**: Keep the `chatgroup` registration and the ownership entry. In
`chat_option_callback`, any `chatgroup` action (either value) answers the callback with the
new `alert.option_removed` text and writes nothing. The settings card is not re-posted, because
nothing changed (FR-007). `mentions_only` presses are treated the same way, since no group-mode
choice exists any more.

**Alternatives considered**: Unregister `chatgroup`. Then the press falls through to the
generic unknown-callback handling with no clear note, which breaks FR-007.

## R6. Observability (FR-011)

**Decision**: `IntakeDecision` gains `reason: IntakeReason | None` with values `private`,
`tagged`, `reply`, `follow_up` (an unaddressed message that joins the sender's open turn) and
`not_addressed` (`store_only`). `IntakeRouter.route` adds `reason=` to the existing `intake`
log line. Ignore and duplicate decisions are not logged, as today. No message text, usernames
or entity offsets are logged.

## R7. Setup and settings text (FR-006)

**Finding**: Setup has no group-mode choice; `setup.pick_prompt` says nothing about groups.
Settings show `settings.chat_group` with a `{mode}` placeholder.

**Decision**: `settings.chat_group` becomes fixed text: en "👥 Drafts in this group: only when
you are tagged or replied to", ru "👥 Черновики в этой группе: только когда вас отметили или
ответили на ваше сообщение". `setup.pick_prompt` gains one sentence (en "In groups I prepare a
draft only when you are tagged or someone replies to your message.", ru "В группах я готовлю
черновик, только когда вас отметили или ответили на ваше сообщение."). Remove
`settings.group_mode.*`, `btn.group_mentions_only`, `btn.group_always`. Add
`alert.option_removed` (en "This option is no longer available", ru "Эта настройка больше
недоступна").
