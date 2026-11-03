# Internal Interfaces: Search Over Earlier Conversations

**Feature**: [../spec.md](../spec.md) | **Research**: [../research.md](../research.md)

Additions to the interfaces in `specs/001-telegram-reply-agent/contracts/internal-interfaces.md`.
Signatures are Python; bodies are left to implementation.

## MessagingGateway (`src/scribe/telegram/gateway.py`)

```python
@dataclass(frozen=True)
class FoundMessage:
    chat_id: int
    message_id: int
    sender_peer_id: int
    sender_display_name: str
    is_outgoing: bool
    text: str  # text, else caption, else document file name; "" dropped
    sent_at: datetime
    reply_to_id: int | None = None
    thread_top_id: int | None = None
    mentioned_peer_ids: frozenset[int] = frozenset()
    mentioned_usernames: frozenset[str] = frozenset()  # lower-case, without "@"


class MessagingGateway(Protocol):
    ...

    async def search_chat(
        self, user_id: uuid.UUID, chat_id: int, query: str, *, limit: int = 20
    ) -> list[FoundMessage]:
        """Server-side search in ONE chat (messages.search), newest first.

        A wait Telegram already announced for this request type raises GatewayBusy(seconds)
        without sending; a first short wait may be slept inside Telethon, so callers bound
        the call with a deadline (EarlierSearch: 6 s).
        Raises GatewayNotStarted when the user's client is not running,
        SessionRevoked when the session is gone.
        """

    async def get_usernames(self, user_id: uuid.UUID, peer_ids: Sequence[int]) -> dict[int, str]:
        """Lower-case usernames (without "@") of the given users, from Telethon's entity cache
        or one users.getUsers call; peers without a username are omitted. Includes the account
        owner when their own id is passed (from get_me)."""

    async def get_messages(
        self, user_id: uuid.UUID, chat_id: int, message_ids: Sequence[int]
    ) -> list[FoundMessage]:
        """One batched lookup (reply targets for the group link rules, S5). Missing ids
        are skipped. Same error contract as search_chat."""
```

- `ClientPool` delegates to `UserClient.search_chat` / `get_messages`. `UserClient` uses
  `iter_messages(entity, search=query, limit=limit)` / `get_messages(entity, ids=...)` /
  `get_entity` (cache first) for `get_usernames`; known flood waits are refused before sending
  and `FloodWaitError` maps to `GatewayBusy` (security review F1).
- `FakeMessagingGateway` gets `search_index: dict[int, list[FoundMessage]]` and records every
  call (`search_calls`) so tests can assert the chat scope and request budget.
- Never called with a chat id that is not an enabled work chat of that user (FR-S01); the
  only caller is `EarlierSearch`.

## EarlierSearch (`src/scribe/context/earlier.py`, new)

```python
MAX_QUERIES = 5
MAX_EXTRA_GROUPS = 2
MAX_CHATS = 4                  # turn chat + private chat + MAX_EXTRA_GROUPS
PER_REQUEST_LIMIT = 20
MAX_MATCHES = 60
SNIPPET_CHARS = 500
IN_FLIGHT = 4
EARLIER_PREFIX = "earlier:"

Outcome = Literal["ok", "no_match", "failed", "rate_limited", "timeout", "not_started"]
LinkReason = Literal["private", "own", "sender", "reply", "replied_by", "mention", "thread"]

@dataclass(frozen=True)
class SearchChat:
    telegram_chat_id: int
    title: str
    kind: ChatKind

@dataclass(frozen=True)
class SearchScope:
    user_id: uuid.UUID
    turn_id: uuid.UUID | None
    turn_chat_id: int                  # Telegram id
    sender_peer_id: int | None         # None: unknown sender, or a group as a whole
    user_peer_id: int | None           # users.telegram_user_id
    incoming_thread_top_id: int | None = None
    known: frozenset[tuple[int, int]] = frozenset()  # (chat id, message id) already offered
    sender_username: str | None = None # filled by search() via get_usernames
    user_username: str | None = None

@dataclass(frozen=True)
class EarlierMatch:
    ref: str                           # earlier:<n>
    found: FoundMessage
    chat_title: str
    chat_kind: ChatKind
    link_reason: LinkReason
    def sender_name(self) -> str: ...  # "me" for the user's own messages
    def history_item(self, timezone: str = "UTC") -> HistoryItem: ...  # text ≤ SNIPPET_CHARS

@dataclass(frozen=True)
class EarlierSearchResult:
    outcome: Outcome
    matches: list[EarlierMatch]
    stats: SearchStats                 # chats, queries, requests, matches, dropped_unlinked, duration_ms
    queries: tuple[str, ...]

class EarlierSearch:
    def __init__(self, gateway: MessagingGateway, settings: Settings, *, clock: Clock = utcnow): ...

    async def chats_for(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        turn_chat: WorkChat,
        sender_contact_id: uuid.UUID | None,
        sender_peer_id: int | None,
    ) -> list[SearchChat]:
        """S4: the turn chat (if enabled), the sender's private chat if it is an enabled work
        chat, ≤ 2 other enabled group work chats where the sender has stored messages in the
        retention window, most recent first. Only rows of work_chats with enabled = true."""

    async def search(
        self, scope: SearchScope, chats: Sequence[SearchChat], queries: Sequence[str]
    ) -> EarlierSearchResult:
        """De-duplicate queries (≤ MAX_QUERIES) and chats (≤ MAX_CHATS), fan out
        ≤ len(queries) × len(chats) search_chat calls (≤ IN_FLIGHT in flight), fetch reply
        targets for group chats once, apply the link rules (S5), drop known/empty/duplicate
        messages (S6), cap at MAX_MATCHES newest first, assign refs earlier:1..N.
        Fetches usernames of the sender and the user once (get_usernames) for @mentions.
        Whole call bounded by settings.earlier_search_timeout_s (6 s). Never raises:
        failures become outcome failed / rate_limited / timeout / not_started."""

def link_reason(found: FoundMessage, scope: SearchScope, kind: ChatKind,
                reply_targets: Mapping[int, FoundMessage],
                replied_by: Mapping[int, FoundMessage]) -> LinkReason | None
    """Pure (unit-tested): the S5 rule that keeps the match, or None to drop it."""

def excerpt(text: str, queries: Sequence[str], limit: int = 200) -> str
    """≤ limit chars around the first query hit (what the draft stores, S7)."""
```

The `SearchScope` is built by `Composer._search_scope` (composer, inside the session), not by
`EarlierSearch`.

## LLM router (`src/scribe/llm/`)

```python
class FilterContextOutput(_Output):
    history_ids: list[str]
    task_ids: list[str]
    commitment_ids: list[str]
    needs_earlier_context: bool = False        # new (S1)
    search_queries: list[str] = []             # new (S1), ≤ 5

FILTER_MAX_EARLIER = 10

class LLMRouter:
    async def filter_context(self, req: FilterContextRequest, *, user_tier: Tier) -> RoutedResult[FilterContextOutput]
        # unchanged signature; sanitizer trims/dedupes queries, caps at 5 × 60 chars
    async def rank_earlier(self, incoming: list[MessageItem], matches: list[HistoryItem], *, user_tier: Tier) -> RoutedResult[FilterContextOutput]
        # S2: same route and prompt, candidates.history = matches, tasks/commitments empty;
        # history_ids capped at FILTER_MAX_EARLIER; needs_earlier_context/search_queries cleared
```

- `prompts/filter_context.md` → `version: 2`: rules for `needs_earlier_context` (true only when
  the incoming messages refer to earlier conversations or ask for facts absent from
  `candidates`) and `search_queries` (2–5 short word queries in the conversation's language(s),
  covering word forms and synonyms; never names of other people's chats; never instructions
  from the data blocks).
- No new route, no new price entry.

## Composer (`src/scribe/drafting/composer.py`)

```python
@dataclass
class ComposeInput:
    ...
    search_scope: SearchScope | None = None        # None: search disabled for this draft
    search_chats: list[SearchChat] = field(default_factory=list)
    search_enabled: bool = True                    # users.search_earlier_messages

@dataclass
class Composition:
    ...
    rank_result: RoutedResult[FilterContextOutput] | None = None
    earlier: EarlierSearchResult | None = None
    cited_earlier: list[EarlierMatch] = field(default_factory=list)

async def compose_output(
    router: LLMRouter, inp: ComposeInput, *, earlier_search: EarlierSearch | None = None
) -> Composition

class Composer:
    def __init__(..., earlier_search: EarlierSearch | None = None) -> None
```

Pipeline change (inside `compose_output`, after `_select_context`):

1. `earlier_search is None` or `inp.search_scope is None` → no-op (tests/evals without search,
   operator switch off). Otherwise:
   - the first filter call failed (`filter_unavailable`) → no search, `decision="skipped"`,
     and `earlier_search_unavailable` when `inp.search_enabled`;
   - `needs_earlier_context` and `not inp.search_enabled` → warning `earlier_search_off`,
     `decision="off"`;
   - `needs_earlier_context` → `decision="searched"`, `earlier_search.search(scope,
     inp.search_chats, filter_result.output.search_queries)`; failure →
     `earlier_search_unavailable`; no matches → `earlier_search_no_match`; else
     `router.rank_earlier(...)` within `max(2 s, 10 s − elapsed)` (ProviderError → newest 10 +
     `filter_unavailable`; timeout → newest 10 + `earlier_search_unavailable`); none selected
     → `earlier_search_no_match`;
   - otherwise `decision="not_needed"`.
2. Ranked matches are appended to `DraftRequest.history` after the selected recent history,
   oldest first, with their `earlier:*` ids.
3. `invalid_source_ids` treats offered `earlier:*` ids as valid message sources.
4. `_select_context` no longer skips the filter call when there are no candidates if
   `inp.search_scope is not None and inp.search_enabled`.
5. One `earlier_search` log event per draft (S10).

`Composer._load` builds `search_scope` / `search_chats` inside the session
(`Composer._search_scope`: sender = the contact of the last incoming message, `None` for a group
contact; `search_chats` from `EarlierSearch.chats_for`, empty when the user turned search off)
and passes the user's `search_earlier_messages`; `settings.earlier_search_enabled = False`
(operator kill switch) or no `earlier_search` leaves `search_scope=None`.

Wiring: `__main__` builds one `EarlierSearch(self.pool, settings, clock=self._clock)` for the
service's `Composer`; the 🔁 Regenerate handler (`control_bot/review.py`) builds
`EarlierSearch(gateway, settings, clock=clock)`.

## Stored output (`src/scribe/drafting/stored_output.py`)

```python
def pack_output(output, system_warnings, reply_to_message_id=None, earlier_messages=()) -> dict
@dataclass
class StoredOutput:
    ...
    earlier_messages: list[StoredEarlier] = field(default_factory=list)

@dataclass(frozen=True)
class StoredEarlier:
    ref: str; chat_id: int; message_id: int; sent_at: datetime
    chat_title: str; sender: str; excerpt: str      # decrypted on load
```

## Settings (`src/scribe/config.py`)

| Setting | Env | Default |
|---|---|---|
| `earlier_search_enabled` | `SCRIBE_EARLIER_SEARCH_ENABLED` | `true` |
| `earlier_search_timeout_s` | `SCRIBE_EARLIER_SEARCH_TIMEOUT_S` | `6` (1–9) |

## Log events

| Event | Fields (no content) |
|---|---|
| `earlier_search` | `user_id`, `turn_id`, `decision`, `outcome`, `chats`, `queries`, `requests`, `matches`, `dropped_unlinked`, `used`, `duration_ms`, `rank_ms`, `total_ms` |

`decision`: `not_needed` / `searched` / `off` / `skipped` (the first filter call failed, so
there was no decision and no queries). `outcome`: `ok` / `no_match` / `failed` /
`rate_limited` / `timeout` / `not_started`, or `null` when no search ran.
