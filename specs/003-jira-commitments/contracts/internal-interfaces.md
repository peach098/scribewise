# Contract: Internal Interfaces (Jira additions)

Extends `specs/001-telegram-reply-agent/contracts/internal-interfaces.md` as implemented on the
base branch (US3 + issue #6). Only additions and changes are listed.

## TaskSource  (`src/scribe/context/tasks/base.py`) — protocol unchanged

`JiraTaskSource` (`src/scribe/context/tasks/jira/source.py`) replaces the stub
`src/scribe/context/tasks/jira.py` and implements `validate`, `list_containers`,
`list_open_tasks`, plus `cached_tasks(user_id)` / `invalidate(user_id)` like Trello.

- `credentials` passed to `validate` / `list_containers` include the non-secret setup fields:
  `{deployment, site_url, email?, api_token? | pat?, pinned_cert?}`.
- `Task` mapping: research J7. `Task` gains `due_is_date: bool = False`; `due_on()` and the
  due candidate in `deadlines.choose_deadline` use `due_at.date()` when it is set.
- `TaskSourceIdentity` gains `time_zone: str | None = None`, `server_version: str | None = None`.
- Errors are subclasses of `TaskSourceUnavailable` with an `error_code`; messages never contain
  tokens or URLs:

| Exception | `error_code` | When |
|---|---|---|
| `JiraAddressInvalid` | `address_invalid` | URL not https, has userinfo/query/fragment, unparsable |
| `JiraUnreachable(blocked=False)` | `unreachable` | DNS failure, refused, timeout |
| `JiraUnreachable(blocked=True)` | `blocked_address` | resolves to a private/loopback/link-local/… address not allowlisted |
| `JiraCertificateUntrusted(cert: CertInfo)` | `cert_untrusted` | TLS verification failed; `CertInfo{subject, issuer, not_after, sha256, pem}` |
| `JiraVersionUnsupported(found, minimum)` | `version_unsupported` | self-hosted below `SCRIBE_JIRA_DC_MIN_VERSION` |
| `JiraAuthFailed(hint)` | `auth_failed` | 401/403 on `/myself`; `hint="classic_token"` when the Cloud gateway rejects but the site accepts |
| `JiraRateLimited(retry_after_s)` | `rate_limited` | 429 / 503 |
| `JiraIssueGone(key)` | `issue_gone` | 403 / 404 on one issue |

`ReadOnlyViolation(method)` is a `RuntimeError` (a programming error, never caught as an outage).

## JiraClient  (`src/scribe/context/tasks/jira/client.py`)

```python
def build_http_client(
    flavor: JiraFlavor, settings: Settings, *, pinned_pem: str | None = None
) -> httpx.AsyncClient: ...


class JiraClient:
    def __init__(self, flavor: JiraFlavor, http: httpx.AsyncClient) -> None: ...
    async def myself(self) -> JiraIdentity: ...
    async def server_info(self) -> ServerInfo | None: ...  # None on Cloud
    async def projects(self) -> list[Container]: ...  # id = key, name
    def search(
        self, jql: str, *, fields: list[str], expand: list[str] = [], limit: int
    ) -> AsyncIterator[dict[str, Any]]: ...
    async def issue(self, key: str, *, fields: list[str]) -> dict[str, Any]: ...  # JiraIssueGone
    async def comment_ids(self, key: str) -> set[str]: ...
```

- `build_http_client`: request event hook raising `ReadOnlyViolation` for any method other than
  GET/HEAD; `follow_redirects=False`; timeouts connect 5 s / read 15 s; the address-guard
  transport (`netguard.GuardedTransport`); the pinned `SSLContext` when `pinned_pem` is set.
  httpx/httpcore loggers stay at WARNING (as in `trello.py`).
- `JiraFlavor` (`flavors.py`): `CloudFlavor`, `SelfHostedFlavor` with `api_base`, `site_url`,
  `auth_headers()`, `api_prefix` (`/rest/api/3` | `/rest/api/2`), `search_path`
  (`/search/jql` + `nextPageToken` | `/search` + `startAt`), `normalize_body(raw) ->
  CommentText` (ADF | wiki), `author_id(user_obj)` (`accountId` | `key`, falling back to
  `name`), `browse_url(key, comment_id=None)`; `flavor_for(config_json, credentials)`.
- Pure helpers: `bodies.adf_to_text`, `bodies.wiki_to_text` → `CommentText{plain, quoted,
  mentions[{id, display_name}]}`; `jql.quote_keys`, `jql.tasks_jql`, `jql.scan_jql`,
  `jql.jql_datetime(dt, tz)`, `jql.parse_version`; `netguard.validate_site_url`,
  `netguard.check_address`; `tls.fetch_leaf_certificate`, `tls.cert_info`,
  `tls.pinned_ssl_context`.

## Comment sync  (`src/scribe/context/tasks/jira/sync.py`)

```python
class JiraCommentSync:
    def __init__(
        self,
        session_factory,
        router: LLMRouter,
        task_sources: TaskSources,
        settings: Settings,
        *,
        clock: Clock = utcnow,
        client_factory: Callable[[TaskSourceConnection], JiraClient] | None = None,
        notifier: Callable[[uuid.UUID, str], Awaitable[None]] | None = None,
    ) -> None: ...
    async def run_due(self) -> int: ...  # job "jira_comments"
    async def sync_user(self, user_id: uuid.UUID) -> SyncReport: ...
    async def verify_all(self) -> int: ...  # job "jira_verify"
    async def verify_user(self, user_id: uuid.UUID) -> SyncReport: ...


class SyncReport(BaseModel):
    issues_read: int = 0
    comments_read: int = 0
    comments_sent_to_llm: int = 0
    created: int = 0
    updated: int = 0
    merged: int = 0
    auto_resolved: int = 0
    unreachable: int = 0
    error: str | None = None
```

`sync_user` order: load connection snapshot (no session held during HTTP) → search (J8) →
per issue: done → `resolve_for_issue(issue_done)`; select the user's new/changed comments
(cursor) → build `CommentCommitmentInput` → `commitments.extract_from_comment(...)` →
upsert cursor → advance watermark, `TaskSources.record_result(ok=True)`; log
`jira_sync_finished` with the report. Any `TaskSourceUnavailable` → `record_result(ok=False)`,
`next_sync_at` from `Retry-After` when rate-limited; never raises into the scheduler.

## Commitments from comments  (`src/scribe/context/comment_commitments.py`), date reconciliation (`src/scribe/context/task_dates.py`)

```python
@dataclass(frozen=True)
class CommentCommitmentInput:
    user_id: uuid.UUID
    issue_key: str
    issue_title: str
    comment_id: str
    comment_url: str
    comment_text: str
    restricted: bool
    posted_at: datetime
    updated_at: datetime
    counterparty: str
    request_text: str | None
    request_author: str | None
    request_at: datetime | None


async def extract_from_comment(
    session_factory, router, item: CommentCommitmentInput, *, timezone: str
) -> CommentOutcome: ...  # created|updated|merged|none|skipped
async def resolve_for_issue(session, user_id, issue_key, *, note, at) -> int: ...
async def mark_comment_gone(session, user_id, comment_id, *, note, at) -> bool: ...
async def mark_issue_unreachable(session, user_id, issue_key) -> int: ...
async def restore(session, user_id, commitment_id) -> bool: ...
def issue_keys_in(text: str, project_keys: Iterable[str]) -> list[str]: ...
def reconcile_task_dates(
    items: list[TaskItem],
    tasks: Mapping[str, Task],
    commitments: Sequence[CommitmentRef],
    timezone: str,
) -> tuple[list[TaskItem], list[str]]: ...  # (items, warnings)
```

- `CommitmentRequest` gains `source: Literal["telegram", "jira_comment"] = "telegram"` and
  `issue: IssueInfo | None = None` (`{key, title}`); `prompts/commitments.md` → `version: 3`.
- `CommitmentRef` gains `issue_key`, `due_at` (promise time) and `source_note`, all
  `exclude=True`: used by `task_dates.reconcile_task_dates` only, never serialized into a prompt.
- `date_mismatch:<json>` warnings carry `{key, dates: [{on, kind, at?}]}` (ISO dates, issue
  key, source kinds `due|comment|description|promise_jira|promise_telegram`; no names or
  comment text) so no extra stored field is needed; `render.py` renders them in the user's
  language (`card.dates_differ`, `card.date_source.*`).
- One open commitment per comment: the most confident grounded item is kept.
- `open_for(..., issue_keys=())` also returns open commitments with `jira_issue_key` in
  `issue_keys`.
- Merge rule (J10): same contact (or same normalized `to_whom` without a contact), equal `due`,
  normalized `what` equal or word overlap ≥ 0.6.

## Task service  (`src/scribe/context/tasks/service.py`) — additions

- Default sources include `JiraTaskSource()`; `get("jira")` no longer raises
  `NotImplementedError`.
- `async def record_result(session_factory, user_id, *, ok: bool) -> None` (public wrapper of
  the existing `_record`) used by the comment sync.

## Draft composition — additions

- After `_with_refined_deadlines`, the composer calls `reconcile_task_dates`; each returned
  warning `date_mismatch:<task ref>` is added to `system_warnings` (allowed prefix), and the
  card renders `📅 Dates differ: <date> (<source>) · <date> (<source>)` from
  `drafts.output_json.date_notes` (stored next to `system_warnings`).
- Candidate commitments include those linked to loaded tasks' keys or keys named in the
  incoming text (only the connection's `project_keys`).

## Scheduler — additions

- `Every(interval: timedelta)` (`next_after(t) = t + interval`; interval must be > 0) joins
  `Schedule = Daily | Weekly | Every`.
- Jobs registered in `__main__.py`: `jira_comments` (`Every(jira_sync_interval_s)`) and
  `jira_verify` (`Daily(03:40 UTC)`).

## Settings  (`src/scribe/config.py`) — additions

| Setting | Default | Purpose |
|---|---|---|
| `SCRIBE_JIRA_DC_MIN_VERSION` | `10.3` | J6 (≥ 8.14) |
| `SCRIBE_JIRA_ALLOWED_PRIVATE_HOSTS` | empty | J4 allowlist (hosts or CIDRs, comma-separated) |
| `SCRIBE_JIRA_SYNC_INTERVAL_S` | `300` | J8 (≥ 60) |
| `SCRIBE_JIRA_SYNC_CONCURRENCY` | `5` | J8 |
| `SCRIBE_JIRA_LOOKBACK_DAYS` | `30` | FR-J07 |
| `SCRIBE_JIRA_FIRST_SCAN_MAX_ISSUES` | `1000` | J8 |
