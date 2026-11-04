"""LLM provider protocol and the request/response models that cross the LLM boundary.

See contracts/internal-interfaces.md. Callers never name a provider or model: they call
``LLMRouter.<route>(request)``; the router turns the request into data :class:`Block` s and hands
them to a :class:`ProviderAdapter` together with the route's system prompt.

Prompt-injection defence (FR-010): user-derived content (messages, history, task titles, contact
names) only ever travels inside *data blocks* of the user turn, never in ``system``. Data blocks
are delimited and marked untrusted; the system prompt is the prompt file alone.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, ClassVar, Literal, Protocol, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

# ``none`` turns thinking off; only adapters with ``Capabilities.disable_thinking`` take it
# (``registry.build_adapters`` refuses a route table that gives it to another provider).
Effort = Literal["none", "low", "medium", "high", "xhigh", "max"]
Tier = Literal["quality", "economy"]
# Request field an OpenAI-compatible server reads the output-token limit from.
MaxTokensParam = Literal["max_completion_tokens", "max_tokens"]
RouteName = Literal[
    "retrieve.filter_context",
    "classify.reply_needed",
    "extract.commitments",
    "extract.deadlines",
    "compose.draft",
    "profile.build",
    "eval.style_judge",
]
ROUTE_NAMES: tuple[RouteName, ...] = (
    "retrieve.filter_context",
    "classify.reply_needed",
    "extract.commitments",
    "extract.deadlines",
    "compose.draft",
    "profile.build",
    "eval.style_judge",
)


# --- errors -------------------------------------------------------------------------------------


class ProviderError(Exception):
    """Base class for adapter failures. Messages never contain prompt or output content."""


class ProviderUnavailable(ProviderError):
    """Network error, rate limit, overload or other API error."""

    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = True):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class ProviderRefused(ProviderError):
    """The model (or its safety classifier) declined the request."""

    def __init__(self, message: str = "model refused", *, category: str | None = None):
        super().__init__(message)
        self.category = category


class ProviderOutputInvalid(ProviderError):
    """The output did not validate against the expected schema (or was truncated).

    ``truncated``: the model stopped at ``max_tokens`` (a lighter request may succeed).
    ``usage`` and ``finish_reason`` are what the provider reported for the failed answer, for
    the ``llm_call`` log line (token counts and the stop reason only, never content).
    """

    def __init__(
        self,
        message: str,
        *,
        truncated: bool = False,
        usage: Usage | None = None,
        finish_reason: str | None = None,
    ):
        super().__init__(message)
        self.truncated = truncated
        self.usage = usage
        self.finish_reason = finish_reason


# --- adapter protocol ---------------------------------------------------------------------------


class Capabilities(BaseModel):
    model_config = ConfigDict(frozen=True)

    structured_outputs: bool
    prompt_caching: bool
    batch: bool
    reasoning_effort: bool
    # Accepts ``effort: none`` (thinking off), e.g. DeepSeek's ``thinking: {type: disabled}``.
    disable_thinking: bool = False


class Block(BaseModel):
    """One part of the user turn: free text or a delimited, untrusted data block."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["text", "data"]
    content: str
    name: str = ""
    stable: bool = False  # part of the cacheable prefix (e.g. the style profile)
    # "user": the user's own instructions (specs/013); only the ``user_rules`` block may say so.
    # Everything else is untrusted content written by other people.
    trust: Literal["untrusted", "user"] = "untrusted"

    @model_validator(mode="after")
    def _only_user_rules_are_trusted(self) -> Block:
        if self.trust == "user" and (self.kind != "data" or self.name != USER_RULES_BLOCK):
            raise ValueError(f"only the {USER_RULES_BLOCK!r} data block can be trust=user")
        return self

    def render(self) -> str:
        if self.kind == "text":
            return self.content
        return f'<data name="{self.name}" trust="{self.trust}">\n{self.content}\n</data>'


def escape_data(content: str) -> str:
    """Write every ``<`` / ``>`` of serialized JSON as the JSON escapes ``\\u003c`` /
    ``\\u003e`` (issue #23): untrusted text can then neither close the ``<data>`` delimiter
    nor open a new one, and ``json.loads`` still returns the original text (``<`` and ``>``
    only occur inside JSON strings, where the escape is valid)."""
    return content.replace("<", "\\u003c").replace(">", "\\u003e")


USER_RULES_BLOCK = "user_rules"


def data_block(
    name: str, payload: Any, *, stable: bool = False, key_order: Sequence[str] = ()
) -> Block:
    """JSON data block, escaped by :func:`escape_data`; ``sort_keys`` keeps the bytes stable for
    prompt caching. ``key_order``: the top-level keys of a dict ``payload`` in this order
    instead (keys inside them stay sorted), so the parts that change least come first."""
    if key_order and isinstance(payload, dict):
        parts = [
            json.dumps(key, ensure_ascii=False)
            + ": "
            + json.dumps(payload[key], ensure_ascii=False, sort_keys=True, default=str)
            for key in key_order
            if key in payload
        ]
        content = "{" + ", ".join(parts) + "}"
    else:
        content = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return Block(
        kind="data",
        name=name,
        content=escape_data(content),
        stable=stable,
        trust="user" if name == USER_RULES_BLOCK else "untrusted",
    )


class Usage(BaseModel):
    input_tokens: int = 0  # uncached input
    output_tokens: int = 0
    cached_input_tokens: int = 0  # read from the prompt cache
    cache_write_input_tokens: int = 0  # written to the prompt cache (5-minute TTL on Anthropic)
    cache_write_1h_input_tokens: int = 0  # written to the prompt cache with the 1-hour TTL
    # Part of ``output_tokens`` the model spent reasoning, when the provider reports it
    # separately (OpenAI-compatible ``completion_tokens_details``); informational, not priced.
    reasoning_tokens: int = 0
    latency_ms: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            cache_write_input_tokens=self.cache_write_input_tokens + other.cache_write_input_tokens,
            cache_write_1h_input_tokens=(
                self.cache_write_1h_input_tokens + other.cache_write_1h_input_tokens
            ),
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            latency_ms=self.latency_ms + other.latency_ms,
        )

    @property
    def cache_hit_tokens(self) -> int:
        """Prompt tokens read from the cache (DeepSeek ``prompt_cache_hit_tokens``)."""
        return self.cached_input_tokens

    @property
    def cache_miss_tokens(self) -> int:
        """Prompt tokens not read from the cache, written to it or not (DeepSeek
        ``prompt_cache_miss_tokens``)."""
        return self.input_tokens + self.cache_write_input_tokens + self.cache_write_1h_input_tokens


OutputT = TypeVar("OutputT", bound=BaseModel)


@dataclass(frozen=True)
class StructuredResult[OutputT: BaseModel]:
    output: OutputT
    usage: Usage
    model: str  # the model that actually served the request (may differ after a fallback)
    batch: bool = False
    fallback_used: bool = False


@dataclass(frozen=True)
class StructuredCall:
    """One request of a batch (same arguments as ``complete_structured``)."""

    model: str
    system: str
    user_blocks: Sequence[Block]
    schema: type[BaseModel]
    effort: Effort | None
    max_tokens: int
    cache_prefix: bool = False


BatchOutcome = StructuredResult[BaseModel] | ProviderError


class ProviderAdapter(Protocol):
    name: str
    capabilities: Capabilities

    async def complete_structured(
        self,
        *,
        model: str,
        system: str,
        user_blocks: Sequence[Block],
        schema: type[OutputT],
        effort: Effort | None,
        max_tokens: int,
        cache_prefix: bool,
    ) -> StructuredResult[OutputT]: ...

    async def complete_structured_batch(
        self, calls: Sequence[StructuredCall]
    ) -> list[BatchOutcome]:
        """Results in request order; a failed item is a ``ProviderError``, not a raise.

        Only called when ``capabilities.batch`` is true.
        """
        ...


# --- request models (router input) --------------------------------------------------------------


class LLMRequest(BaseModel):
    """Base for route requests: every field becomes one data block of the user turn, in field
    declaration order (``STABLE_FIELDS`` first). Issue #70: requests declare what changes least
    first and the incoming messages last, so consecutive calls share a long byte-identical
    prefix for the provider's prompt cache."""

    STABLE_FIELDS: ClassVar[frozenset[str]] = frozenset()
    # Field name -> the order of its top-level keys in the block (default: sorted).
    KEY_ORDER: ClassVar[dict[str, tuple[str, ...]]] = {}

    def to_blocks(self) -> list[Block]:
        data = self.model_dump(mode="json")

        def block(key: str, value: Any, stable: bool = False) -> Block:
            return data_block(key, value, stable=stable, key_order=self.KEY_ORDER.get(key, ()))

        stable = [block(k, v, True) for k, v in data.items() if k in self.STABLE_FIELDS]
        volatile = [block(k, v) for k, v in data.items() if k not in self.STABLE_FIELDS]
        return stable + volatile


class _Forwardable(BaseModel):
    """#47: ``forwarded`` marks a forwarded copy (its text is not ``sender``'s own words) and
    ``forwarded_from`` names its origin when known. Both keys are left out of the serialized
    item when it is not a forward, so ordinary items read exactly as before."""

    forwarded: bool = False
    forwarded_from: str | None = None

    @model_serializer(mode="wrap")
    def _omit_when_not_forwarded(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if not self.forwarded:
            data.pop("forwarded", None)
            data.pop("forwarded_from", None)
        elif self.forwarded_from is None:
            data.pop("forwarded_from", None)
        self._omit_defaults(data)
        return data

    def _omit_defaults(self, data: dict[str, Any]) -> None:
        """Hook for subclasses with more keys that are left out at their default."""


class MessageItem(_Forwardable):
    id: str
    sender: str
    text: str
    # When it was sent, in the user's time zone (ISO 8601 with offset); None in old fixtures.
    sent_at: datetime | None = None
    # Specs/013: a document, photo or link preview is attached (the reply gate sees only the
    # text). Left out of the serialized item when false, so other requests read as before.
    has_media: bool = False

    def _omit_defaults(self, data: dict[str, Any]) -> None:
        if not self.has_media:
            data.pop("has_media", None)


class HistoryItem(_Forwardable):
    id: str
    is_user: bool
    sender: str
    text: str
    sent_at: datetime


class TaskItem(BaseModel):
    id: str
    title: str
    status: str
    due: date | None = None  # the effective deadline (due field, or a comment / description)
    assignees: list[str] = Field(default_factory=list)
    # Where ``due`` comes from and, for a comment or the description, a short note with the
    # author, the comment's date and the quoted words ("from a comment by …, Sat 18 Oct: “…”").
    deadline_source: Literal["due", "comment", "description"] | None = None
    deadline_note: str | None = None


class CommitmentRef(BaseModel):
    id: str
    what: str
    due: date | None = None
    # Jira (specs/003-jira-commitments J12): the issue the promise is about, when it was made
    # (the newest promise wins over the issue's other dates) and where it was found. Used by
    # ``context/task_dates.py`` only: never serialized into a prompt.
    issue_key: str | None = Field(default=None, exclude=True)
    due_at: datetime | None = Field(default=None, exclude=True)
    source_note: str | None = Field(default=None, exclude=True)


class FilterCandidates(BaseModel):
    # Issue #70: serialized in this order (``FilterContextRequest.KEY_ORDER``): tasks and
    # commitments change rarely, the anchored history only grows at its tail.
    tasks: list[TaskItem] = Field(default_factory=list)
    commitments: list[CommitmentRef] = Field(default_factory=list)
    history: list[HistoryItem] = Field(default_factory=list)


class FilterContextRequest(LLMRequest):
    """Issue #70: the candidates first, the incoming messages (new every turn) last."""

    KEY_ORDER: ClassVar[dict[str, tuple[str, ...]]] = {
        "candidates": ("tasks", "commitments", "history")
    }

    candidates: FilterCandidates
    incoming_messages: list[MessageItem]


class ContactInfo(BaseModel):
    display_name: str
    kind: Literal["person", "group"]


class ReplyNeededRequest(LLMRequest):
    """Reply gate (004 contracts/reply-gate.md): does this turn need a reply at all?"""

    # Specs/013: the user's own rules for this contact (``AppliedRules.user_rules_json()``);
    # ``{}`` when there are none. The only block rendered ``trust="user"``.
    user_rules: dict[str, Any] = Field(default_factory=dict)
    incoming_messages: list[MessageItem]  # the turn (or the single new message while pending)
    history: list[HistoryItem] = Field(default_factory=list)  # last <= skip_context_messages
    contact: ContactInfo
    pending_draft: bool = False


class ProfileContext(BaseModel):
    rules_json: dict[str, Any] = Field(default_factory=dict)
    examples: list[str] = Field(default_factory=list)


class ChatOverrides(BaseModel):
    language: str | None = None
    formality: int | None = Field(default=None, ge=1, le=5)


class UserStateContext(BaseModel):
    ooo: str | None = None


class UserContext(BaseModel):
    """Facts about the user that hold for every draft, whichever profile is selected (issue #46).

    ``grammatical_gender``: the form for words about the user in languages with grammatical
    gender; a hard constraint over examples, rules and chat overrides. None: not set (the model
    follows the examples)."""

    grammatical_gender: Literal["masculine", "feminine"] | None = None


class DraftRequest(LLMRequest):
    """Blocks in declaration order (issue #70): the profile (the cached prefix on Anthropic),
    then what holds for the user or the chat, then the turn's selected context, then the date
    anchor, and the incoming messages last."""

    STABLE_FIELDS: ClassVar[frozenset[str]] = frozenset({"profile", USER_RULES_BLOCK})

    profile: ProfileContext = Field(default_factory=ProfileContext)
    # Specs/013: the user's own rules, style and behaviour (``AppliedRules.user_rules_json()``),
    # a block of their own right after the profile (``trust="user"``, same cached prefix).
    user_rules: dict[str, Any] = Field(default_factory=dict)
    # Outside ``profile`` (issue #46): the same for the global and every contact profile.
    user: UserContext = Field(default_factory=UserContext)
    contact: ContactInfo
    chat_overrides: ChatOverrides = Field(default_factory=ChatOverrides)
    user_state: UserStateContext = Field(default_factory=UserStateContext)
    # The user's IANA time zone (with ``today`` the date anchor).
    timezone: str | None = None
    commitments: list[CommitmentRef] = Field(default_factory=list)
    tasks: list[TaskItem] = Field(default_factory=list)
    history: list[HistoryItem] = Field(default_factory=list)
    # Date anchor: the user's local date of the newest incoming message. Relative dates
    # ("в среду", "завтра", "до конца недели") resolve against ``today``.
    today: date | None = None
    # Issue #75: the next CALENDAR_DAYS days from ``today`` with their weekdays, so the model
    # names a weekday by looking it up instead of working it out ("by mon" for a Tuesday due).
    calendar: list[CalendarDay] = Field(default_factory=list)
    incoming_messages: list[MessageItem]

    @model_validator(mode="after")
    def _fill_calendar(self) -> DraftRequest:
        if self.today is not None and not self.calendar:
            self.calendar = calendar_from(self.today)
        return self


CALENDAR_DAYS = 14


class CalendarDay(BaseModel):
    date: date
    weekday: str  # English name, e.g. "Wednesday"


def calendar_from(today: date, days: int = CALENDAR_DAYS) -> list[CalendarDay]:
    """``today`` and the following days, each with its English weekday name."""
    return [
        CalendarDay(date=day, weekday=day.strftime("%A"))
        for day in (today + timedelta(days=offset) for offset in range(days))
    ]


class StyleSample(BaseModel):
    text: str
    sent_at: datetime
    contact_display_name: str | None = None
    # Where the message was written (Telegram chat id; a group or channel-comments chat is
    # ``group``): the global profile's sample selection limits group chats (issue #30). Used by
    # ``style/profiles.py`` only: never serialized into a prompt.
    chat_id: int | None = Field(default=None, exclude=True)
    group: bool = Field(default=False, exclude=True)


class StyleProfileRequest(LLMRequest):
    samples: list[StyleSample]
    scope: Literal["global", "contact"]
    existing_user_rules_json: dict[str, Any] = Field(default_factory=dict)


class IssueInfo(BaseModel):
    key: str
    title: str


class CommitmentRequest(LLMRequest):
    """``source="jira_comment"`` (specs/003 J9): ``outgoing_text`` is the user's own Jira
    comment (quotes removed), ``recent_incoming`` at most the one request it answers, ``issue``
    the issue it is on. Telegram requests carry neither ``source`` nor ``issue`` blocks."""

    outgoing_text: str
    contact_display_name: str
    sent_at: datetime
    recent_incoming: list[MessageItem] = Field(default_factory=list)
    source: Literal["telegram", "jira_comment"] = "telegram"
    issue: IssueInfo | None = None

    def to_blocks(self) -> list[Block]:
        blocks = super().to_blocks()
        if self.source == "telegram":  # unchanged prompt input for Telegram messages
            return [b for b in blocks if b.name not in {"source", "issue"}]
        return blocks


class DeadlineText(BaseModel):
    id: str  # "c0", "c1", ... (comments) or "desc"
    kind: Literal["comment", "description"]
    author: str = ""
    # When it was written, in the user's time zone (ISO 8601 with offset): the date anchor.
    written_at: datetime | None = None
    text: str


class DeadlineTask(BaseModel):
    id: str
    title: str
    due: date | None = None  # the card's own due field, in the user's time zone
    texts: list[DeadlineText]


class DeadlineRequest(LLMRequest):
    timezone: str
    tasks: list[DeadlineTask]


class StyleJudgeRequest(LLMRequest):
    draft_text: str
    profile_summary: str
    reference_examples: list[str] = Field(default_factory=list)


# --- output models (structured outputs; every field required, no extra keys) -------------------


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FilterContextOutput(_Output):
    history_ids: list[str]
    task_ids: list[str]
    commitment_ids: list[str]
    # specs/004 (research S1): whether the reply needs messages older than the candidates, and
    # the short word queries to find them with. Optional so older outputs stay valid.
    needs_earlier_context: bool = Field(
        default=False,
        description=(
            "True only when the incoming messages refer to earlier conversations or ask for a"
            " fact that is not in the candidates."
        ),
    )
    search_queries: list[str] = Field(
        default_factory=list,
        description=(
            "When needs_earlier_context is true: 2-5 short word queries (word forms, synonyms,"
            " the conversation's languages) to search earlier messages. Otherwise empty."
        ),
    )


class Placeholder(_Output):
    marker: str = Field(description="Exact [[...]] token used in text.")
    question: str = Field(description="What the user must supply.")


class Source(_Output):
    kind: Literal["message", "task", "commitment"]
    id: str
    used_for: str = Field(description="Short note of which claim this supports.")


class CreatesCommitment(_Output):
    value: bool
    what: str | None
    due: str | None = Field(description="ISO date if the draft promises a date.")


SensitiveCategory = Literal["credentials", "payment", "personal_data", "legal", "hr"]


class Sensitive(_Output):
    value: bool
    categories: list[SensitiveCategory]


class DraftFlags(_Output):
    creates_commitment: CreatesCommitment
    sensitive: Sensitive


class Conflict(_Output):
    with_kind: Literal["task", "commitment"]
    with_id: str
    description: str


ContextWarning = Literal["tasks_unavailable", "weak_style_profile", "history_truncated"]


class DraftOutput(_Output):
    """Structured output returned by LLMProvider.draft_reply. Used as output_config.format for the drafting call and validated by the composer."""  # noqa: E501 - must equal the JSON schema text

    text: str = Field(
        description=(
            "The proposed reply, ready to send."
            " Placeholders appear verbatim as [[...]] in the text."
        )
    )
    language: str = Field(description="BCP-47 language code of the draft, e.g. 'ru', 'en'.")
    needs_info: bool = Field(
        description="True when the reply could not be completed from the available context."
    )
    placeholders: list[Placeholder]
    sources: list[Source] = Field(
        description=(
            "Every fact, date, number or commitment in the text must be backed by at least one"
            " source id taken from the request (message ids, task ids, commitment ids)."
        )
    )
    flags: DraftFlags
    conflicts: list[Conflict] = Field(
        description="Incoming request vs existing commitments or task deadlines."
    )
    tone_shift_applied: Literal["none", "more_formal", "urgent", "emotional"]
    context_warnings: list[ContextWarning]


class StyleRules(_Output):
    """Learned style rules (research R8)."""

    languages: list[str] = Field(description="BCP-47 codes, most used first.")
    formality: int = Field(ge=1, le=5, description="1 = very casual, 5 = very formal.")
    typical_length_words: int = Field(ge=0)
    greetings: list[str]
    sign_offs: list[str]
    emoji_usage: Literal["none", "rare", "frequent"]
    punctuation_habits: list[str]
    typical_phrases: list[str]
    never_does: list[str]


class StyleProfileOutput(_Output):
    rules_json: StyleRules
    summary_text: str
    chosen_example_indices: list[int]


class CommitmentItem(_Output):
    what: str
    to_whom: str
    due: date | None = Field(description="ISO date, or null when no date was promised.")
    confidence: float = Field(ge=0, le=1)
    evidence: str = Field(description="Short quote from the outgoing message.")


class CommitmentExtraction(_Output):
    items: list[CommitmentItem]


class DeadlineItem(_Output):
    task_id: str
    text_id: str = Field(description="The id of the comment or description that gives the date.")
    source: Literal["comment", "description"]
    deadline_at: date = Field(description="The deadline as an ISO date (YYYY-MM-DD).")
    quote: str = Field(description="The exact words of that text that give the deadline.")
    confidence: float = Field(ge=0, le=1)


class DeadlineExtraction(_Output):
    items: list[DeadlineItem]


class ReplyNeededOutput(_Output):
    """Structured output of the classify.reply_needed route (004 research R2)."""

    reply_needed: bool = Field(
        description=(
            "True when the user would normally answer: any question, request, new information"
            " calling for a response, disagreement, complaint, a greeting that opens a"
            " conversation, or a user rule that asks to draft this turn."
        )
    )
    category: Literal["thanks", "farewell", "acknowledgment", "informational", "none"] = Field(
        description=(
            "What the turn mainly is when it needs no reply: a closing, or 'informational' when"
            " it only shares news, a status, an announcement, a document, a link or reference"
            " data and asks nothing; 'none' when it does something else."
        )
    )
    confidence: float = Field(ge=0, le=1, description="Confidence in reply_needed.")
    resolves_pending: bool = Field(
        description=(
            "Only meaningful when pending_draft is true in the request: the turn says the earlier"
            " request is no longer needed. False otherwise."
        )
    )
    decided_by: Literal["default", "user_rule"] = Field(
        description=(
            "'user_rule' when a rule in the user_rules block settled reply_needed (in either"
            " direction); 'default' when the built-in rules did."
        )
    )


class StyleJudgeOutput(_Output):
    score: float = Field(ge=0, le=1)
    reasons: list[str]


ROUTE_OUTPUT_SCHEMAS: dict[RouteName, type[BaseModel]] = {
    "retrieve.filter_context": FilterContextOutput,
    "classify.reply_needed": ReplyNeededOutput,
    "extract.commitments": CommitmentExtraction,
    "extract.deadlines": DeadlineExtraction,
    "compose.draft": DraftOutput,
    "profile.build": StyleProfileOutput,
    "eval.style_judge": StyleJudgeOutput,
}
