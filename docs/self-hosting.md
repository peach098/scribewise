# Self-hosting guide

This guide takes you from an empty server to a running Scribe, then covers models, upgrades,
backups and troubleshooting. The short version is in the [README](../README.md#quick-setup).

- [Requirements](#requirements)
- [Install](#install)
- [Keep your bot private](#keep-your-bot-private)
- [Choosing models](#choosing-models)
- [Upgrading](#upgrading)
- [Backups](#backups)
- [Logs and health](#logs-and-health)
- [Troubleshooting](#troubleshooting)

## Requirements

- A Linux server or a computer that stays on, with Docker and Docker Compose. The models run
  at your LLM provider, so a small server is enough for a few users.
- Outbound access to Telegram, to your LLM provider and, if you use them, to Jira or Trello.
  No inbound ports are needed: the bot uses long polling.
- A Telegram bot token from [@BotFather](https://t.me/BotFather). This bot becomes your
  review chat.
- A Telegram API id and hash from [my.telegram.org](https://my.telegram.org) → API
  development tools. Set the app title to **Scribe Agent**, so the session is easy to spot in
  Telegram's *Active sessions*.
- An API key for an LLM: [Anthropic](https://console.anthropic.com/) (the default) or any
  OpenAI-compatible API, including a model you host yourself. See
  [Choosing models](#choosing-models).

Run **one instance** per database and bot token: two processes would share the Telegram
sessions, and Telegram would end them.

Python and uv are needed only to run Scribe outside Docker or to work on the code.

## Install

```bash
git clone https://github.com/peach098/scribewise.git
cd scribewise
cp .env.example .env
chmod 600 .env
openssl rand -base64 32        # paste the output as SCRIBE_MASTER_KEY
```

In `.env`, fill in `SCRIBE_MASTER_KEY`, `SCRIBE_BOT_TOKEN`, `SCRIBE_TG_API_ID`,
`SCRIBE_TG_API_HASH` and your LLM key (`ANTHROPIC_API_KEY`, or `OPENAI_API_KEY` with the
settings in [Choosing models](#choosing-models)).

> **Keep `SCRIBE_MASTER_KEY` safe and backed up.** It encrypts stored messages, style
> profiles, chat and contact names, Telegram sessions and integration tokens. Without it the
> database is unreadable, and there is no way to recover it. If it may have leaked, replace it
> as described in [Rotating the master key](configuration.md#rotating-the-master-key).

Start the database, apply the migrations and start the app:

```bash
docker compose up -d db
docker compose run --rm app alembic upgrade head
docker compose up -d --build app
curl -s localhost:8080/healthz     # {"status": "ok", ...}
```

If port 5432 or 8080 is already taken on the host, set `SCRIBE_DB_HOST_PORT` or
`SCRIBE_HEALTH_HOST_PORT` in `.env`. Both are published on `127.0.0.1` only. Do not change
`SCRIBE_HEALTH_PORT`: it is the port inside the container, and the compose file expects 8080.

Then open your bot in Telegram, send `/start` and follow the
[connection steps](../README.md#4-connect-your-account).

### Running outside Docker

For development, run Postgres in Docker and the app with uv. Scribe reads LLM keys and
`SCRIBE_LLM__*` overrides from the process environment, so pass `.env` explicitly:

```bash
docker compose up -d db
uv sync
export SCRIBE_DATABASE_URL=postgresql+asyncpg://scribe:scribe@localhost:5432/scribe
uv run --env-file .env alembic upgrade head
uv run --env-file .env python -m scribe
```

The exported `SCRIBE_DATABASE_URL` overrides the one in `.env`, which points at the `db`
container.

## Keep your bot private

Without the access gate **anyone who finds your bot's username could send `/start`**, connect
their own Telegram account and use your LLM key. So `.env.example` turns the gate on, and you
only list yourself as the operator:

```bash
SCRIBE_ACCESS_GATE=true
SCRIBE_OPERATOR_TELEGRAM_IDS=<your numeric Telegram id>
```

Everyone else gets a short "closed beta" reply. You can find your id with any "user info" bot
in Telegram. If the operator list is empty, Scribe still starts (and logs
`access_gate_no_operator`), but nobody gets in until you add yourself with
`docker compose run --rm app python -m scribe.admin beta add <your id>`. The same command lets
a colleague in. See [access-gate.md](access-gate.md) for the full set of commands.

With the gate off, `python -m scribe` **refuses to start** (exit code 2, "the access gate is
off, so anyone who finds the bot could use it") unless you also set `SCRIBE_OPEN_BOT=true`.
That says, on purpose, that the bot serves every sender. `python -m scribe.admin` and
`alembic` keep working either way, so you can add users before you start the service.

## Choosing models

Scribe never names a model in code. Each task is a *route*, and
[`config/llm_routes.yml`](../config/llm_routes.yml) maps every route to a provider and a
model:

| Route | Used for | Default model |
| --- | --- | --- |
| `retrieve.filter_context` | picking the relevant history, tasks and promises | Claude Haiku |
| `classify.reply_needed` | deciding whether "thanks!" needs a reply | Claude Haiku |
| `extract.commitments` | finding promises in your messages and Jira comments | Claude Haiku |
| `extract.deadlines` | reading dates from task comments | Claude Haiku |
| `compose.draft` (`quality` / `economy` tiers) | writing the draft | Claude Opus / Sonnet |
| `profile.build` | learning your style | Claude Opus |
| `eval.style_judge` | the evaluation set only | Claude Haiku |

At startup Scribe checks the table and refuses to start if:

- a route uses an unknown provider;
- a model is missing from [`config/llm_prices.yml`](../config/llm_prices.yml) (prices are used
  only for cost figures in logs and `python -m scribe.admin cost`, so `0.0` is fine for a
  self-hosted model);
- a route's provider does not have `data_use_confirmed: true`. By setting it, you confirm that
  the provider does not train on the data you send. It is `true` for Anthropic's API and
  `false` for `openai_compat` until you set it.

Users get the `quality` tier by default. Switch one user to the cheaper tier with
`python -m scribe.admin set-tier <user id> economy`.

### Changing a single value

Any value can be overridden from `.env` with `SCRIBE_LLM__<ROUTE>__<FIELD>`, where the route
name is written in upper case with `_` instead of `.`:

```bash
SCRIBE_LLM__COMPOSE_DRAFT__QUALITY__MODEL=claude-sonnet-5-5
SCRIBE_LLM__PROFILE_BUILD__EFFORT=medium
SCRIBE_LLM__PROVIDERS__OPENAI_COMPAT__DATA_USE_CONFIRMED=true
```

### Using only an OpenAI-compatible API

To run without Anthropic (for example with a vLLM, Ollama or LiteLLM server, or another
hosted API), write your own route table. Copy the default and point every route at
`openai_compat`:

```yaml
# config/llm_routes.local.yml
routes:
  retrieve.filter_context: {provider: openai_compat, model: my-model, effort: low,    max_tokens: 8000}
  classify.reply_needed:   {provider: openai_compat, model: my-model, effort: low,    max_tokens: 8000}
  extract.commitments:     {provider: openai_compat, model: my-model, effort: low,    max_tokens: 8000}
  extract.deadlines:       {provider: openai_compat, model: my-model, effort: low,    max_tokens: 8000}
  compose.draft:
    tiers:
      quality:             {provider: openai_compat, model: my-model, effort: medium, max_tokens: 16000}
      economy:             {provider: openai_compat, model: my-model, effort: medium, max_tokens: 16000}
  profile.build:           {provider: openai_compat, model: my-model, effort: high,   max_tokens: 32000}
  eval.style_judge:        {provider: openai_compat, model: my-model, effort: low,    max_tokens: 8000}
providers:
  openai_compat:
    api_key_env: OPENAI_API_KEY
    base_url_env: OPENAI_BASE_URL
    data_use_confirmed: true      # you confirm the server does not train on your data
    native_json_schema: true      # false if the server rejects response_format json_schema
    reasoning_effort: false       # true to send each route's effort as reasoning_effort
    max_tokens_param: max_completion_tokens  # max_tokens if the server ignores max_completion_tokens
    disable_thinking: false       # true if the server takes DeepSeek's thinking: {type: disabled}
```

A route's `effort` is one of `low`, `medium`, `high`, `xhigh`, `max`, or `none`. `none` turns
thinking off; only an `openai_compat` provider with `disable_thinking: true` takes it (Scribe
sends `thinking: {type: disabled}` and no `reasoning_effort`), and Scribe refuses to start if
a route on any other provider, Anthropic included, asks for it. Leave `effort` out to use the
server's own default.

`max_tokens_param` names the request field that carries each route's `max_tokens`. OpenAI
expects `max_completion_tokens` (its `max_tokens` is deprecated), which is the default. Some
OpenAI-compatible servers, DeepSeek among them, document only `max_tokens` and silently ignore
the other field, so the route limits would not apply; set `max_tokens_param: max_tokens` for
those.

Add `my-model` to `config/llm_prices.yml`, then set in `.env`:

```bash
SCRIBE_LLM_ROUTES_PATH=config/llm_routes.local.yml
OPENAI_API_KEY=...
OPENAI_BASE_URL=https://my-llm.example.com/v1
```

`config/` is copied into the Docker image, so rebuild after editing it:
`docker compose up -d --build app`.

### DeepSeek

[`config/llm_routes.deepseek.yml`](../config/llm_routes.deepseek.yml) is a complete, ready
table for DeepSeek's API (it is also the table of the evaluation set): `deepseek-flash` for
filtering, classification and extraction, `deepseek-v4-pro` for drafts and style profiles. It
is the cheapest hosted option, but
read DeepSeek's [privacy policy](https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html)
first: data is stored in the People's Republic of China, and user input may be used for
training unless you opt out (by writing to privacy@deepseek.com); the
[Open Platform terms](https://cdn.deepseek.com/policies/en-US/deepseek-open-platform-terms-of-service.html)
do not exclude API data from that. For that reason the table ships with
`data_use_confirmed: false`, and Scribe refuses to start on it until you attest it yourself
with the environment override below. Do that only after you have opted out, or decided the
data may be used this way, and accept the data location:

```bash
SCRIBE_LLM_ROUTES_PATH=config/llm_routes.deepseek.yml
DEEPSEEK_API_KEY=...
DEEPSEEK_BASE_URL=https://api.deepseek.com
SCRIBE_LLM__PROVIDERS__OPENAI_COMPAT__DATA_USE_CONFIRMED=true
```

The table turns thinking off (`effort: none`) where a short structured answer needs no
reasoning: the reply gate (`classify.reply_needed`) and the filter, which also ranks the
earlier-conversation matches (`retrieve.filter_context`). The other routes think at the
effort they list. With thinking on, `max_tokens` counts the reasoning too, so do not lower it
on a route that thinks without checking the evaluation set's largest output for that route.

Cost figures use DeepSeek's list prices and its off-peak half price (calls that start outside
01:00-04:00 and 06:00-10:00 UTC on weekdays); Chinese public holidays are priced as peak.

Smaller models write weaker drafts and may struggle with the style profile build. If a
profile build is cut off, lower `SCRIBE_PROFILE_MAX_SAMPLES` (400 by default) or raise the
route's `max_tokens`.

## Upgrading

Migrations are **not** applied automatically. To upgrade:

```bash
git pull
docker compose build app
docker compose exec -T db pg_dump -U scribe -d scribe -Fc > backup-$(date +%F).dump
docker compose stop app
docker compose run --rm app alembic upgrade head
docker compose up -d app
```

Read the [release notes](../release-notes/) first: they say when a version needs something
extra. Run one instance per database: a second `python -m scribe` against the same database
exits with code 1 without starting anything. After an update to a new version the bot sends each user a short "what's new" message
in their language; set `SCRIBE_RELEASE_NOTES_AUTO=false` to turn this off (see
[release-notes.md](release-notes.md)).

## Backups

Back up two things, and keep them in different places:

1. **The database:**
   `docker compose exec -T db pg_dump -U scribe -d scribe -Fc > scribe-$(date +%F).dump`.
   Restore with the app stopped:
   `docker compose exec -T db pg_restore -U scribe -d scribe --clean < scribe-<date>.dump`.
2. **`SCRIBE_MASTER_KEY`** from `.env`. A dump without the key cannot be decrypted, and the
   key without a dump has nothing to decrypt.

The database holds the Telegram sessions too (encrypted). After a restore everyone stays
signed in, unless a user ended the session in Telegram in the meantime.

## Logs and health

- Logs are JSON lines on stdout: `docker compose logs -f app`. Set the level with
  `SCRIBE_LOG_LEVEL` (`INFO` by default). Logs contain no message texts at any level,
  `DEBUG` included: the LLM, Telegram and HTTP libraries stay at `INFO` or higher whatever
  the level. Only `SCRIBE_LOG_LIBRARIES_DEBUG=true` (false by default) lets them log their
  requests at `DEBUG`, content included, so use it only briefly to troubleshoot.
- `GET /healthz` returns `{"status": "ok", "users_active": N, "access_gate": false}` with HTTP
  200, or `"status": "degraded"` with HTTP 503 when the database is unreachable, the scheduler
  has stalled or the bot has stopped polling. Point your uptime monitor at it.
- `/history` in the bot shows what the agent did for you, without message content.

## Troubleshooting

**The app exits right after start.** Exit code 2 means a configuration error; the last log
line says which. The usual causes: a missing or malformed `SCRIBE_MASTER_KEY` (it must be
base64 of exactly 32 bytes), the access gate turned off without `SCRIBE_OPEN_BOT=true`
([Keep your bot private](#keep-your-bot-private)), an LLM key variable that is not set, a model
missing from `llm_prices.yml`, or a provider without `data_use_confirmed: true`. Exit code 1
with "another instance is already running" means another Scribe uses the same database.

**`environment variable ANTHROPIC_API_KEY is not set` outside Docker.** Run with
`uv run --env-file .env …`; see [Running outside Docker](#running-outside-docker).

**The login code does not work.** Telegram blocks a login code that was forwarded or pasted
as is. Type it with spaces between the digits (`1 2 3 4 5`), or use the QR code.

**No drafts appear.** Check that the chat is enabled in `/chats`, that it is within your
working hours (`/settings`), and that drafting is not paused. `/waiting` shows messages that
are still waiting to be read, and `/skipped` shows messages that got no draft because they
needed no reply. In groups Scribe drafts only when you are mentioned or someone replies to
you.

**`SCRIBE_WORKING_DAYS` is rejected.** It takes a JSON list, 0 for Monday:
`SCRIBE_WORKING_DAYS=[0,1,2,3,4]`. The `.env` values for hours, days and delays are defaults
for new users only; existing users change theirs in `/settings`.

**Self-hosted Jira on a private network is refused.** Scribe blocks private addresses to
prevent server-side request forgery. Allow your Jira explicitly:
`SCRIBE_JIRA_ALLOWED_PRIVATE_HOSTS=jira.corp.example,10.20.0.0/16`.

**The session was terminated in Telegram.** The bot tells you and offers **Reconnect** in
`/telegram`. Settings, chats and style profiles are kept.
