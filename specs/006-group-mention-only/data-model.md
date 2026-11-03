# Data Model: Group Chats — Draft Only When Addressed

Changes against `specs/001-telegram-reply-agent/data-model.md`. No new tables or columns.

## Removed

| Object | Where | Was |
|--------|-------|-----|
| Column `work_chats.group_mode` | `src/scribe/db/models.py` (`WorkChat.group_mode`) | NOT NULL, default `mentions_only` (001 FR-003) |
| Postgres enum `group_mode` (`mentions_only`, `always`) | created in `alembic/versions/0001_initial.py` | Values of the column above |
| Python enum `GroupMode` | `src/scribe/db/models.py` | Mirror of the Postgres enum |
| `ChatRule.group_mode` | `src/scribe/intake/router.py` | Copied from the work chat for `decide` |

**Work Chat** (001) after the change: a group work chat has no drafting mode. Per-chat
overrides are language, formality, delay, skip-closing (004) and enabled. Every group follows
the intake rule below.

## Unchanged fields, sharpened meaning

`IncomingMessage` (`src/scribe/telegram/gateway.py`) keeps its fields. Their meaning is
pinned down (research R1–R3):

| Field | Meaning after this feature |
|-------|----------------------------|
| `mentions_me` | The server set `mentioned` on the user's copy **and** the message is not a forward (`fwd_from is None`). Covers `@username` (`MessageEntityMention`) and name-link (`MessageEntityMentionName`) tags of the user's account. |
| `is_reply_to_me` | Groups only: the message's reply header is a real reply in this chat (not the implicit forum-topic header `forum_topic and reply_to_top_id is None`, not a reply to another peer), and the replied-to message is an ordinary message (`action is None`) sent by the user (`out`). |
| `reply_to_id` | Unchanged (raw `reply_to_msg_id`, stored on `messages.reply_to_telegram_message_id`). |

## New value type (in-process only, not stored)

`IntakeReason` (`StrEnum` in `src/scribe/intake/router.py`):

| Value | When |
|-------|------|
| `private` | Private work chat: always addressed (FR-009) |
| `reply` | Group, `is_reply_to_me` (checked first) |
| `tagged` | Group, `mentions_me` and not `is_reply_to_me` |
| `follow_up` | Group, not addressed, but joins the same sender's open waiting turn in the grouping window (FR-008) |
| `not_addressed` | Group, not addressed, no turn to join, so `store_only` (FR-003) |

`IntakeDecision` gains `reason: IntakeReason | None` (None for `ignore`/`duplicate`). The
reason is logged and never stored.

## Intake rule (replaces 001 FR-003)

```text
address_reason(message, rule):
  private chat            -> private
  is_reply_to_me          -> reply
  mentions_me             -> tagged
  otherwise               -> None

decide(message, rule, open_turn):
  ignore (unchanged)      when chat unknown/disabled or message outgoing
  reason = address_reason(...)
  joins  = open waiting turn of the same sender within grouping_window_s
  reason is None and not joins -> store_only (not_addressed)
  media only                   -> handle_manually (reason)
  joins                        -> attach (reason or follow_up)
  else                         -> new_turn (reason)
```

The DB-backed branches after `decide` (answered, attach_pending, attach_reading) run only for
`new_turn` / `handle_manually`, so an unaddressed message never marks a pending draft outdated
(spec edge case). This is the current behaviour and stays as is.

## Migration `0009_drop_group_mode`

`alembic/versions/0009_drop_group_mode.py`, `revision = "0009"`, `down_revision = "0008"`
(re-chain if another branch lands `0009` first).

- **upgrade**: `op.drop_column("work_chats", "group_mode")`, then
  `sa.Enum(name="group_mode").drop(bind, checkfirst=True)`. No row update is needed: the drop
  removes the `always` setting for every chat (FR-005).
- **downgrade**: create the enum `group_mode ('mentions_only', 'always')`, add
  `work_chats.group_mode` NOT NULL with `server_default='mentions_only'`, then drop the server
  default (0001 had none). Former `always` chats come back as `mentions_only`; that loss is
  accepted and stated in the module docstring.
- **Test**: round trip `0008 → 0009 → 0008 → head` on a scratch database, with one chat
  inserted as `always` at 0008. It must be gone after upgrade and `mentions_only` after
  downgrade. `alembic check` must stay clean.
