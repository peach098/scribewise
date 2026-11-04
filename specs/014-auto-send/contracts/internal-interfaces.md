# Internal Interfaces: Auto-Send for Trusted Chats

Signatures are the contract; bodies are for implementation. All new code is in
`src/scribe/sending/auto_send.py` unless noted.

## 1. Eligibility (pure, no I/O)

```python
class BlockReason(StrEnum):
    hard_stop = "hard_stop"; commitment = "commitment"; sensitive = "sensitive"
    placeholders = "placeholders"; dates_differ = "dates_differ"; conflict = "conflict"
    weak_style = "weak_style"; out_of_office = "out_of_office"; warning = "warning"
    no_output = "no_output"; outdated = "outdated"; chat_off = "chat_off"
    outside_hours = "outside_hours"; suspended = "suspended"; paused = "paused"
    not_first_version = "not_first_version"

AUTO_SEND_ALLOWED_WARNINGS: frozenset[str] = frozenset()

@dataclass(frozen=True)
class Eligibility:
    reasons: tuple[BlockReason, ...]
    @property
    def eligible(self) -> bool: ...

def eligibility(
    *, draft: Draft, output: DraftOutput | None, system_warnings: Sequence[str],
    context_warnings: Sequence[str], chat: WorkChat, user: User,
    fire_at: datetime | None = None,   # set at fire time: adds the working-hours check
) -> Eligibility: ...
```

Rules: research A4. Fail closed: unknown warning → `warning`. Never raises. Deterministic for
the same inputs (unit-tested table, SC-001).

## 2. Arming (called once, when a first-version card is posted)

```python
async def arm(session: AsyncSession, *, draft_id: UUID, now: datetime) -> Eligibility | None:
    """Lock the draft (FOR UPDATE), then its chat and user (FOR SHARE).
    Returns None when the chat has auto-send off (card is ordinary, no line).
    Eligible: set drafts.auto_send_due_at = now + users.auto_send_window_s.
    Not eligible: leaves due_at NULL; the caller renders card.auto_send_manual."""
```

Call site: `scheduling/scheduler.py:DueTurnsProcessor`, after `composer.compose` commits and
before `presenter.post_card`; `DraftCardView` gains `auto_send_at: datetime | None` and
`auto_send_reasons: tuple[str, ...]`.

## 3. Scheduler step

```python
# scheduling/scheduler.py
def on_auto_sends(self, handler: DraftStepHandler) -> None: ...
# _step_auto_sends: _draft_step(handler,
#     Draft.auto_send_due_at <= now, Draft.auto_send_stop.is_(None),
#     Draft.auto_approved_at.is_(None), Draft.outdated.is_(False))
# runs after _step_due_turns and before _step_reminders.

class AutoSender:
    def __init__(self, session_factory, gateway, presenter: DraftPresenter, *,
                 clock: Clock, marker: UnreadMarker | None,
                 commitments: CommitmentExtractor | None,
                 advisor: AutoSendAdvisor) -> None: ...
    async def __call__(self, session: AsyncSession, drafts: Sequence[Draft]) -> AfterCommit | None:
        """In the step's transaction, per draft: eligibility(fire_at=now).
        Eligible: auto_approved_at = now; activity auto_approved.
        Blocked: auto_send_stop = <mapped reason>; activity auto_send_stopped.
        AfterCommit: send_approved_draft(..., auto=True) for fired drafts, then
        presenter.show_status(status.sent_auto) and advisor.after_decision; on failure
        mark_send_failed + presenter.show_manual(reason=send_failed).
        Blocked drafts: presenter.show_manual(reasons)."""
```

Registered in `__main__.py` next to `register_review_deadlines`.

## 4. Send path changes (`sending/send.py`)

```python
async def send_approved_draft(..., auto: bool = False) -> SendResult
```

- `_check_sendable`: `approved_by_user_at is None and auto_approved_at is None` → refuse.
- `auto=True` additionally requires `auto_approved_at is not None` and refuses `edited_text`.
- On success: `ActivityKind.sent` meta gains `auto`; writes the `draft_decisions` row
  (`auto_sent` if `auto` else `sent_unchanged`/`sent_edited`, `during_auto_send =
  auto_send_due_at is not None`) in the same transaction.
- The docstring sentence "called only from the approve/edit callbacks" becomes "from the
  approve/edit callbacks and from `AutoSender`".

## 5. Stopping a countdown

```python
def stop_countdown(draft: Draft, reason: AutoSendStop) -> bool:
    """On a row the caller holds FOR UPDATE. True if it was armed and is now stopped.
    False if not armed, already stopped, or already fired (auto_approved_at set)."""

def is_firing(draft: Draft) -> bool  # auto_approved_at set and state pending

async def stop_chat_countdowns(session, *, user_id: UUID, work_chat_ids: Sequence[UUID] | None,
                               reason: AutoSendStop) -> list[UUID]:
    """Conditional UPDATE for all armed, not-fired pending drafts of the chats (None = all
    chats of the user). Returns draft ids to re-render."""
```

Call sites in `telegram/control_bot/review.py`: `on_approve`, `on_edit`, `on_reject`,
`on_regen`, `on_fill_values` call `stop_countdown(..., user_action)` in their existing locked
transaction; if `is_firing` they answer `autosend.too_late` and return. New handler
`on_autostop`. `intake/router.py:_flag_pending_draft` calls `stop_countdown(..., outdated)`.

## 6. Settings and chat state

```python
async def set_chat_auto_send(session, *, user_id: UUID, work_chat_id: UUID, on: bool,
                             source: AutoSendSource | None, now: datetime,
                             reason: str = "user") -> list[UUID]:
    """Locks the work chat. on=True requires enabled and not excluded (else LocalizedError
    autosend.chat_not_active). on=False also stops countdowns. Records auto_send_on/off.
    Returns draft ids to re-render."""

async def turn_off_everywhere(session, *, user_id: UUID, now: datetime) -> list[UUID]
```

`chats/work_account.py`: `deactivate_chat`, `exclude_chat`, `switch(off)` set
`WorkChat.auto_send = False` and record `auto_send_off` with their reason in the same
transaction.

## 7. Decisions and advisor

```python
# db/repos/decisions.py
async def record(session, *, user_id, work_chat_id, draft_id, decision: DraftDecisionKind,
                 during_auto_send: bool, at: datetime) -> None
async def chat_rate(session, *, work_chat_id: UUID, since: datetime) -> ChatRate  # unchanged, total
async def recent_auto_outcomes(session, *, work_chat_id: UUID, limit: int) -> list[bool]  # True = intervened
async def purge_older_than(session, cutoff: datetime) -> int

class AutoSendAdvisor:
    def __init__(self, session_factory, bot, *, clock: Clock) -> None: ...
    async def after_decision(self, user_id: UUID, work_chat_id: UUID) -> None:
        """Never raises (logs autosend_advisor_failed). Offer or warn per research A7."""
```

Call sites (after commit): `_approve_and_send`, `on_reject`, `on_regen`, `on_autostop`,
`AutoSender` after-commit.

## 8. Logs (structured, content-free)

| Event | Fields |
|---|---|
| `auto_send_armed` | `user_id`, `draft_id`, `due_at` |
| `auto_send_blocked` | `user_id`, `draft_id`, `reasons`, `phase` (`arm`/`fire`) |
| `auto_send_stopped` | `user_id`, `draft_id`, `reason` |
| `auto_send_fired` | `user_id`, `draft_id` |
| `sent` (existing) | + `auto` |
| `auto_send_changed` | `user_id`, `work_chat_id`, `on`, `source`/`reason` |
| `auto_send_offered` / `auto_send_warned` | `user_id`, `work_chat_id`, `unchanged`, `total` / `interventions` |
| `autosend_advisor_failed` | `user_id`, `work_chat_id`, `error` |

## 9. Fakes and test support

- `FakeMessagingGateway` unchanged (sends already recorded as `SendTextCall`).
- `tests/integration/us2_support.py`: helper `arm_chat(harness, chat)` and
  `decisions(harness, chat, unchanged=…, edited=…, rejected=…)`.
