# Contract: Group Intake Rule

Internal interface (`src/scribe/intake/router.py`, `src/scribe/telegram/user_client.py`).
It replaces `is_addressed` (001 FR-003).

## Pure functions

```python
class IntakeReason(enum.StrEnum):
    private = "private"
    reply = "reply"
    tagged = "tagged"
    follow_up = "follow_up"
    not_addressed = "not_addressed"


@dataclass(frozen=True)
class ChatRule:
    enabled: bool
    kind: ChatKind  # group_mode removed


@dataclass(frozen=True)
class IntakeDecision:
    action: Action
    turn_id: uuid.UUID | None = None
    reason: IntakeReason | None = None


def address_reason(message: IncomingMessage, chat: ChatRule) -> IntakeReason | None: ...
def decide(message, chat, open_turn, *, grouping_window_s) -> IntakeDecision: ...
```

Guarantees:
- Private chats: every incoming message is addressed (`private`); behaviour unchanged.
- Groups: `new_turn` / `handle_manually` only when `address_reason` is not None. `attach` also
  for an unaddressed message from the same sender within the grouping window
  (`follow_up`). Everything else is `store_only` (`not_addressed`).
- `reply` wins over `tagged` when both flags are set.
- No chat setting can make an unaddressed group message open a turn.

## Mapping helpers (`user_client.py`)

```python
def replies_to_message(message) -> bool
    # True iff reply_to is a MessageReplyHeader with reply_to_msg_id, not the bare
    # forum-topic header (forum_topic and reply_to_top_id is None), and reply_to_peer_id is
    # None or this chat.

def mentions_me(message) -> bool
    # bool(message.mentioned) and message.fwd_from is None

def is_own_message(reply) -> bool
    # reply is not None and reply.out and getattr(reply, "action", None) is None
```

`_handle_new_message` calls `get_reply_message()` only when the chat is a group and
`replies_to_message(message)` is true.

## Log line

`intake` (structlog, info), emitted for every decision except `ignore`, as today:

| Field | Value |
|-------|-------|
| `user_id` | str |
| `action` | `Action` value |
| `reason` | `IntakeReason` value or null (new) |
| `chat_kind` | `private` / `group` / … |
| `turn_id` | str or null |

No message text, sender names, usernames or entity data.
