# Configuration

Scribe is configured with environment variables, normally in `.env` (start from
[`.env.example`](../.env.example)). All settings are defined in
[`src/scribe/config.py`](../src/scribe/config.py); startup fails with exit code 2 on a missing
or invalid value. LLM models are configured separately, see
[Choosing models](self-hosting.md#choosing-models).

Lists such as `SCRIBE_OPERATOR_TELEGRAM_IDS` are comma-separated, except
`SCRIBE_WORKING_DAYS`, which is a JSON list (`[0,1,2,3,4]`, 0 is Monday). Times are `HH:MM`.

## Required

| Variable | Meaning |
| --- | --- |
| `SCRIBE_DATABASE_URL` | Postgres URL, `postgresql+asyncpg://user:password@host:5432/db` |
| `SCRIBE_MASTER_KEY` | Base64 of 32 random bytes (`openssl rand -base64 32`); encrypts stored data. Back it up; to replace it see [Rotating the master key](#rotating-the-master-key) |
| `SCRIBE_BOT_TOKEN` | Control bot token from @BotFather |
| `SCRIBE_TG_API_ID`, `SCRIBE_TG_API_HASH` | Telegram API credentials from my.telegram.org |
| `ANTHROPIC_API_KEY` | Needed only if a route uses the `anthropic` provider (the default) |
| `OPENAI_API_KEY`, `OPENAI_BASE_URL` | Needed only if a route uses `openai_compat` |

## Defaults for new users

Each user changes these in `/settings`; the values below apply only to accounts created after
you set them.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SCRIBE_DELAY_MIN_S` / `SCRIBE_DELAY_MAX_S` | 120 / 600 | Random reading delay range, seconds (set both to 0 for quick tests) |
| `SCRIBE_WORK_START` / `SCRIBE_WORK_END` | 09:00 / 19:00 | Working hours, in the user's time zone |
| `SCRIBE_WORKING_DAYS` | `[0,1,2,3,4]` | Working days, 0 is Monday |

## Drafting and review

| Variable | Default | Meaning |
| --- | --- | --- |
| `SCRIBE_GROUPING_WINDOW_S` | 60 | Messages from one person within this window get one draft |
| `SCRIBE_DRAFT_REMIND_AFTER_S` | 7200 | One reminder about a draft nobody answered |
| `SCRIBE_DRAFT_EXPIRE_AFTER_S` | 86400 | After this the draft expires and the message is left to you |
| `SCRIBE_REGENERATE_MAX_PER_DRAFT` | 3 | 🔁 Regenerate at most this many times per draft; new messages in the conversation start the count again |
| `SCRIBE_EDIT_PREFILL` | `auto` | ✏️ Edit: `auto` fills the input field when inline mode is on in BotFather, else offers the text to copy; `inline` / `copy` force one way |
| `SCRIBE_READ_CONCURRENCY` | 50 | Messages marked read in parallel |
| `SCRIBE_COMPOSE_CONCURRENCY` | 10 | Drafts written in parallel |
| `SCRIBE_TEST_FAST_CLOCK` | false | Reminder after 1 minute and expiry after 2, for manual tests |

## Skipping messages that need no reply

| Variable | Default | Meaning |
| --- | --- | --- |
| `SCRIBE_SKIP_MIN_CONFIDENCE` | 0.8 | Classifier confidence needed to skip a message |
| `SCRIBE_SKIP_CANDIDATE_MAX_WORDS` | 15 | Longer messages are never skipped |
| `SCRIBE_SKIP_CONTEXT_MESSAGES` | 8 | Earlier messages shown to the classifier |
| `SCRIBE_SKIP_HABIT_WINDOW` | 5 | Past closing messages checked for the user's habit |
| `SCRIBE_SKIP_HABIT_MIN_REPLIES` | 3 | If the user answered this many of them, the next one gets a draft |
| `SCRIBE_SKIP_HABIT_REPLY_WINDOW_MIN` | 30 | A message sent within this many minutes counts as a reply |
| `SCRIBE_SKIPPED_LIST_DAYS` | 7 | How far back `/skipped` looks |

## Daily LLM budget per user

| Variable | Default | Meaning |
| --- | --- | --- |
| `SCRIBE_LLM_DAILY_SOFT_CAP_USD` | 1.00 | From this much LLM spend in a day, the user's drafts use the `economy` tier of `compose.draft`. 0 turns it off |
| `SCRIBE_LLM_DAILY_HARD_CAP_USD` | 3.00 | From this much, drafting pauses for the user until the next day. 0 turns it off |

A day is a UTC day, the same day `python -m scribe.admin cost` shows. Spend is the
`cost_usd` of every billed call made for the user, priced from `SCRIBE_LLM_PRICES_PATH`; calls on
a model priced at 0 never count. The soft cap must not be above the hard cap.

- **Soft cap.** Drafts use the route table's `economy` tier for `compose.draft` (in the default
  table Sonnet instead of Opus; in `llm_routes.deepseek.yml` `deepseek-flash` instead of
  `deepseek-v4-pro`). Drafts are the main cost, and they are the only route with tiers, so the
  other routes stay as they are. The user's own tier (`python -m scribe.admin set-tier`) is not
  changed: the next day drafts use it again. The user is not told.
- **Hard cap.** No LLM call is made for new messages: a due message is read as usual, then
  skipped without asking the reply gate and listed in `/skipped` with the reason "daily AI limit
  reached". 🔁 Regenerate, ✍️ Draft anyway and 🔄 Rebuild of the style profiles answer with an
  alert, and a message that arrives while a draft is pending marks the draft outdated without
  asking the classifier. The user gets one notice per UTC day in the control chat saying that
  drafting resumes automatically at midnight UTC (shown in their time zone). The next day
  everything works again; skipped messages can be drafted from `/skipped`.
- **Not paused at the hard cap:** extracting promises from the user's own sent messages and
  Jira comments (cheap, limited by what the user writes, and never retried, so a skipped
  extraction would lose a promise), the weekly profile refresh (it runs on Sunday at 02:00 UTC,
  at the start of a new budget day) and the first profile build during setup.
- The check runs before each message, with one indexed query. A draft already being written
  finishes, so a day can end a little above the hard cap.

**Why these defaults.** The cost model in
[specs/012-llm-cost-optimization/research.md](../specs/012-llm-cost-optimization/research.md)
(§5) puts a typical active user (40 incoming work messages a day, about 23 drafts) at roughly
$0.3 a day on the DeepSeek route table, with drafts as most of it. The soft cap of $1 is about
3 times that, so only an unusually busy day moves to the cheaper model; the hard cap of $3, about
10 times typical, stops runaway loops or abuse while a heavy but normal day still fits. **With
the default Anthropic route table** a typical user costs about $1.2 a day (Opus drafts), so these
defaults would move most users to Sonnet every day: raise them, for example to a soft cap of $4
and a hard cap of $12.

**Operator alert.** Every day at 00:20 UTC each operator (`SCRIBE_OPERATOR_TELEGRAM_IDS`) gets
one message about the UTC day that just ended, in their language: users above the 95th
percentile of that day's spend, users who reached the hard cap and users who reached the soft
cap, by `users.id` with the amount. It holds no names, chats or message content. The percentile
(nearest rank, over users with any spend) is listed only when at least 20 users spent
something that day: with fewer, one or two heavy users decide it and it says nothing, so the
message says it was skipped. Nothing is sent when no user is in any list.
`python -m scribe.admin cost` marks each day on which a user reached a cap.

## Style profiles

| Variable | Default | Meaning |
| --- | --- | --- |
| `SCRIBE_STYLE_IMPORT_MONTHS` | 6 | History read to learn the style |
| `SCRIBE_MAX_CONTACT_PROFILES` | 20 | Per-contact profiles per user |
| `SCRIBE_MIN_STYLE_SAMPLES` | 50 | Fewer own messages than this mark the profile as weak |
| `SCRIBE_PROFILE_MAX_SAMPLES` | 400 | Own messages sent to the model per profile build; lower it for a smaller model |
| `SCRIBE_PROFILE_GROUP_SAMPLES_PER_CHAT` | 40 | At most this many messages from one group chat in the global profile's sample |
| `SCRIBE_PROFILE_GROUP_SAMPLES_SHARE` | 0.3 | Maximum share of group chats in that sample |
| `SCRIBE_PROFILE_EXAMPLES_CONTACT` / `SCRIBE_PROFILE_EXAMPLES_GLOBAL` | 20 / 50 | Example messages kept in a contact / the global profile |
| `SCRIBE_WORK_ACCOUNT_IMPORT_MAX_CHATS` | 50 | Recent private chats the style import reads in work account mode |

A truncated `profile.build` answer is retried once with half the samples and one step lower
effort.

## Telegram login

| Variable | Default | Meaning |
| --- | --- | --- |
| `SCRIBE_TG_QR_MAX_CODES` | 5 | QR code refreshes per login attempt |
| `SCRIBE_TG_LOGIN_PASSWORD_TIMEOUT_S` | 600 | How long the bot waits for the 2FA password |
| `SCRIBE_TG_QR_SAME_DEVICE_LINK` | false | Also offer a link to approve the login on the same phone; check it on iOS and Android first |
| `SCRIBE_TG_DEVICE_MODEL` | `Scribe Agent` | Device name in Telegram's *Active sessions* |
| `SCRIBE_TG_SYSTEM_VERSION` | `server` | System version shown there |
| `SCRIBE_TG_APP_VERSION` | package version | App version shown there |

## Jira

| Variable | Default | Meaning |
| --- | --- | --- |
| `SCRIBE_JIRA_DC_MIN_VERSION` | 10.3 | Oldest self-hosted Jira accepted; raise to 11.3 after 10.3 LTS leaves support on 2026-12-05 |
| `SCRIBE_JIRA_ALLOWED_PRIVATE_HOSTS` | empty | Hosts or CIDRs a self-hosted Jira may use on a private network (when Scribe runs inside that network); empty refuses private addresses |
| `SCRIBE_JIRA_ALLOWED_PORTS` | `443,8443` | The only ports a new Jira address may use; others are refused, so the connection check cannot be used to probe hosts |
| `SCRIBE_JIRA_SYNC_INTERVAL_S` | 300 | How often your Jira comments are checked for promises (at least 60) |
| `SCRIBE_JIRA_SYNC_CONCURRENCY` | 5 | Users synced in parallel |
| `SCRIBE_JIRA_LOOKBACK_DAYS` | 30 | How far back comments are read |
| `SCRIBE_JIRA_FIRST_SCAN_MAX_ISSUES` | 1000 | Issues read on the first scan |

Each user gets at most 5 connection checks in 10 minutes (kept in memory, reset on restart).
Network failures (DNS, refused, timeout, private address, TLS) all get one message; the detail
is in the `jira_connect_rejected` log event.

## Search over earlier conversations

| Variable | Default | Meaning |
| --- | --- | --- |
| `SCRIBE_EARLIER_SEARCH_ENABLED` | true | Operator switch; users also have one in `/settings` |
| `SCRIBE_EARLIER_SEARCH_TIMEOUT_S` | 6 | Time limit of the Telegram search step, 1–9 seconds |

Details: [earlier-search.md](earlier-search.md).

## Access gate

| Variable | Default | Meaning |
| --- | --- | --- |
| `SCRIBE_ACCESS_GATE` | false (`.env.example`: true) | Serve only operators, beta users and subscribers. Keep it on for a personal install too, see [Keep your bot private](self-hosting.md#keep-your-bot-private) |
| `SCRIBE_OPERATOR_TELEGRAM_IDS` | empty | Telegram ids that always have access and decide applications |
| `SCRIBE_OPEN_BOT` | false | With the gate off, `python -m scribe` refuses to start (exit code 2) unless this is true: the bot then serves anyone who finds it, with your LLM key. `python -m scribe.admin` and `alembic` run either way |
| `SCRIBE_SUBSCRIPTION_CONTACT` | empty | Where to arrange a subscription, shown with the **Subscription** button |
| `SCRIBE_ACCESS_REFUSAL_COOLDOWN_S` | 3600 | How often one refused person is answered |

Details: [access-gate.md](access-gate.md).

## Encryption keys

| Variable | Default | Meaning |
| --- | --- | --- |
| `SCRIBE_MASTER_KEY_ID` | `v1` | Id of `SCRIBE_MASTER_KEY`, written into each ciphertext. Every value stored before key rotation existed carries `v1`, so leave it unset until your first rotation |
| `SCRIBE_MASTER_KEYS_PREVIOUS` | empty | Old keys that only decrypt, as `<id>:<base64 key>,<id>:<base64 key>` |
| `SCRIBE_ENCRYPTION_STRICT` | false | See [Strict mode](#strict-mode) |

Configuration errors name the setting, never its value.

## Release notes

| Variable | Default | Meaning |
| --- | --- | --- |
| `SCRIBE_RELEASE_NOTES_AUTO` | true | Send "what's new" to all users at the first start of a new version |
| `SCRIBE_RELEASE_NOTES_RATE_PER_S` | 10 | Messages a second, 1–25 |
| `SCRIBE_RELEASE_NOTES_MAX_ATTEMPTS` | 5 | Network or server errors before a delivery is given up |
| `SCRIBE_RELEASE_NOTES_DIR` | `release-notes` | Where the notes live |

Details: [release-notes.md](release-notes.md).

## Service

| Variable | Default | Meaning |
| --- | --- | --- |
| `SCRIBE_LOG_LEVEL` | INFO | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. Third-party libraries stay at INFO or above (HTTP clients at WARNING) whatever the level, so even DEBUG logs carry no message content |
| `SCRIBE_LOG_LIBRARIES_DEBUG` | false | Let the Telegram, bot and LLM libraries log at DEBUG too. Their DEBUG output contains message content: turn it on only to troubleshoot, then off again |
| `SCRIBE_MESSAGE_RETENTION_DAYS` | 30 | Stored messages are deleted after this |
| `SCRIBE_LLM_SPEND_RETENTION_DAYS` | 90 | Daily per-user LLM spend (shown by `python -m scribe.admin cost`) is deleted after this many days |
| `SCRIBE_SCHEDULER_TICK_S` | 5 | How often the scheduler looks for due work |
| `SCRIBE_HEALTH_HOST` / `SCRIBE_HEALTH_PORT` | 0.0.0.0 / 8080 | Health endpoint inside the container (Compose publishes it on 127.0.0.1 only); leave the port as is with Docker Compose. `/healthz` has no authentication: outside a container set the host to `127.0.0.1` or keep the port private |
| `SCRIBE_LLM_ROUTES_PATH` | `config/llm_routes.yml` | LLM route table |
| `SCRIBE_LLM_PRICES_PATH` | `config/llm_prices.yml` | Model prices, used for cost figures: the `cost_usd` of each `llm_call` log line and the per-user daily spend shown by `python -m scribe.admin cost`. Supports 1-hour cache write prices (`cache_write_1h_input_per_m`, default 2 x input) and time-of-day discounts (`off_peak`, used for DeepSeek's off-peak half price) |
| `SCRIBE_PROMPTS_DIR` | `prompts` | Prompt templates |
| `SCRIBE_LLM__<ROUTE>__<FIELD>` | — | Override one value of the route table, e.g. `SCRIBE_LLM__COMPOSE_DRAFT__QUALITY__MODEL=claude-sonnet-5-5`, or a provider's, e.g. `SCRIBE_LLM__PROVIDERS__OPENAI_COMPAT__DATA_USE_CONFIRMED=true`; see [Choosing models](self-hosting.md#choosing-models). `SCRIBE_LLM__PROVIDERS__ANTHROPIC__CACHE_TTL` (`1h` by default, or `5m`) sets the lifetime of the Anthropic prompt-cache entries for the draft and filter system prompts and the style profile: 1-hour writes cost 2 x input, 5-minute ones 1.25 x |

Docker Compose only (not read by the app):

| Variable | Default | Meaning |
| --- | --- | --- |
| `SCRIBE_DB_HOST_PORT` | 5432 | Host port for Postgres, on 127.0.0.1 |
| `SCRIBE_HEALTH_HOST_PORT` | 8080 | Host port for `/healthz`, on 127.0.0.1 |

Tests only: `SCRIBE_TEST_DATABASE_URL` points the integration tests at an existing Postgres
instead of starting one with testcontainers.

## Rotating the master key

Rotate the key when it may have leaked, or on your own schedule. No data is lost: until the
old key is removed, values under either key can be read. Run the commands in the app container
(`docker compose run --rm app ...`).

1. **Generate a new key:** `openssl rand -base64 32`. Pick a new id that has not been used
   before, e.g. `v2`. Ids use `A-Z a-z 0-9 _ . -` and are at most 32 characters; `e2` is
   reserved.
2. **Set the new key as current and keep the old one as previous** in `.env`:

   ```bash
   SCRIBE_MASTER_KEY=<new key>
   SCRIBE_MASTER_KEY_ID=v2
   SCRIBE_MASTER_KEYS_PREVIOUS=v1:<old key>   # add older ones too if any value still uses them
   ```

   If `SCRIBE_MASTER_KEY_ID` was unset, the old key's id is `v1`.
3. **Restart** (`docker compose up -d`). New values now use `v2`; old ones are still read with
   `v1`. Jira comment fingerprints come from the current key, so each Jira comment is checked
   once more after the restart.
4. **Re-encrypt everything** to the new key: `python -m scribe.jobs reencrypt_all`. It works
   in batches, so the service can keep running, and you can stop it at any time and run it
   again: it continues with the values that are not done yet. It prints a count per column.
   (The service also runs it every day at 04:30 UTC.)
5. **Check:** `python -m scribe.jobs reencrypt_all --check` writes nothing and must report
   `total: to rewrite=0 failed=0` (exit status 0). `failed` counts values that cannot be read
   with any configured key; the `reencrypt_value_failed` log lines name their table, column
   and row id. Do not remove a key while values still fail or are pending.
6. **Remove the old key:** delete its entry from `SCRIBE_MASTER_KEYS_PREVIOUS`, restart, and
   run `--check` once more. Also delete or re-protect database backups made under the old key
   if it leaked: those backups can still be read with it.

## Strict mode

Every ciphertext written now is bound to its table and column, so a value copied by someone
with write access to the database into another column fails to decrypt; a Telegram session
copied into another user's row is refused too. Values stored before this binding existed (and
plaintext left in columns that were encrypted later) are still read as they are, so an update
needs no action.

To close that gap, run `python -m scribe.jobs reencrypt_all` (step 4 above; with the current
key it only upgrades the format) until `--check` reports nothing to rewrite, then set
`SCRIBE_ENCRYPTION_STRICT=true` and restart. Scribe then refuses plaintext in encrypted
columns, ciphertexts in the old format and Telegram sessions not bound to their user, and the
re-encryption jobs no longer adopt such values. Strict mode is off by default because turning
it on before the re-encryption has finished would make old rows unreadable.
