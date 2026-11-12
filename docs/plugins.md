# Plugins

A plugin is a Python module that adds to Scribe without changing its code: more control-bot
handlers, per-user usage limits, more operator commands, more sections in `/my_data`. List the
modules in `SCRIBE_PLUGINS` (comma-separated); the module must be importable by the service
(installed in its environment or on its `PYTHONPATH`).

With `SCRIBE_PLUGINS` empty (the default) nothing on this page applies and Scribe behaves
exactly as without it.

## Loading

At startup `python -m scribe` imports each module in order and calls its `setup(host)` once,
after the database, the bot, the Telegram client pool and the scheduler exist and before the
control bot starts. A module that cannot be imported, has no `setup`, or whose `setup` raises
stops the startup with a configuration error (exit code 2).

```python
from scribe.plugins import PluginHost


def setup(host: PluginHost) -> None:
    host.add_messages("en", {"example.hello": "Hello from the plugin"})
    host.add_messages("ru", {"example.hello": "Привет от плагина"})
```

`host` gives the service's shared objects, `settings`, `session_factory`, `bot`, `scheduler`
(register a job with `host.scheduler.register_job`), `clock` and `pool` (the Telegram client
pool), and the registration methods below.

## Extension points

### Texts

`host.add_messages(lang, {key: text})` adds texts for `en` or `ru` to the control bot's catalog;
plural entries work as in `scribe/i18n`. A key that already exists, in the core or from another
plugin, is a startup error. Use a prefix of your own for the keys.

### Usage limits per user

`host.set_limits_resolver(resolver)` sets what each user may use. The resolver has one method,
`async limits_for(session, user, now) -> UsageLimits` (`scribe.drafting.budget`):

| Field | Default | Effect |
| --- | --- | --- |
| `caps` | the deployment's | The user's daily soft and hard LLM caps, used everywhere the caps apply: the composer, the reply gate, the scheduler's check before each turn, Regenerate, Draft anyway, Rebuild, the operators' spend alert and `python -m scribe.admin cost` |
| `max_drafts_per_day` | `None` | Once this many drafts were created in a UTC day, the next due turns are skipped as `budget` before the reply gate (no model call) and the user gets one notice that day (`budget.drafts_notice`) |
| `max_work_chats` | `None` | Enabling a chat in `/chats`, the ➕ picker and the setup picker are refused at this many enabled chats (`chats.limit_reached`); work account mode cannot be turned on, and its pick-up adds no chat |
| `profile_refresh_allowed` | `True` | `False`: the automatic refresh skips the user, 🔄 Rebuild answers `profile.rebuild_unavailable` and the `/settings` switch shows "not available". The first build at setup still runs |
| `profile_refresh_default` | `True` | Whether the automatic refresh is on for a user who has not chosen in `/settings` |

The default resolver gives every user the deployment caps and no other limit, without a
database query. Only one plugin may set a resolver. The routes in
`SCRIBE_LLM_BUDGET_EXCLUDED_ROUTES` are never counted, whatever caps the resolver returns.

The core never changes a user's chats or modes when their limits change. A plugin that lowers a
limit applies it itself with the core helpers (`scribe.db.repos.chats.set_enabled`,
`scribe.chats.work_account.switch`); `host.on_limits_changed(fn)` and
`await host.limits_changed(user_id)` let parts of a plugin tell each other.

### Notices

`host.wrap_budget_notifier(fn)` decorates the once-a-day "drafting paused" notice and the
"access ends soon" reminder of the access gate. `fn` is
`async (user_id, msg) -> (msg, buttons)`, where `msg` is a `scribe.i18n.Msg` and `buttons` is
`None` or a function from the user's language to an inline keyboard. Several decorators apply
in the order they were added.

### Control-bot routers

`host.add_router(router, first=False)` adds an aiogram router to the control bot. With
`first=True` it goes before every core router, so its handlers answer first, also for a core
callback such as `apply:subinfo`; otherwise after them. `host.handle_pre_checkout(router)` adds a
router with `pre_checkout_query` handlers (the core registers none).

### Access gate

With the access gate on, people without access reach only a few handlers. A plugin can open
more paths for them with `host.gate_allow(commands=..., callback_prefixes=..., message_filter=...)`:
commands (without the slash), callback data prefixes and a function `Message -> bool`. Updates
that match reach the handlers for refused and restricted senders alike and do not count against
the refusal cooldown.

`host.set_no_access_offer(fn)` replaces the **Join the beta** / **Subscription** buttons of the
"no access" replies. `fn` is `async (telegram_id, lang, session) -> list of button rows`; the
same buttons follow the usage-limit refusals above. Only one plugin may set it.

### Personal data

- `host.on_my_data(fn)`: `fn(session, telegram_id, lang) -> list[str]`, sections appended to the
  `/my_data` summary.
- `host.on_delete_warning(fn)`: same signature, lines added to the `/delete_my_data` question.
- `host.on_delete_user(fn)`: `fn(session, telegram_id)`, called inside the transaction that
  deletes the user, after the core rows are gone; delete the plugin's rows of that person there.
  If it raises, nothing is deleted and the user is asked to try again.

### Access events

A plugin records its own entries in the access log with
`scribe.access.repo.record_plugin_event(session, telegram_id, "event_name", actor=..., now=...)`:
`kind` is `plugin` and the name is kept in `meta_json["event"]`.
`python -m scribe.admin access show` prints it as `plugin:event_name`.

### Operator commands

`python -m scribe.admin` reads `SCRIBE_PLUGINS` too and, for each module with a
`register_admin(subparsers)` function, adds its commands. The function adds its subcommands to
the `argparse` subparsers and returns `{command name: handler}`, where a handler is
`async (args, session_factory, settings, clock) -> exit code`. A command name taken twice is an
error.

```python
import argparse


def register_admin(subparsers: argparse._SubParsersAction) -> dict:
    subparsers.add_parser("example", help="a plugin command")

    async def example(args, session_factory, settings, clock) -> int:
        print("done")
        return 0

    return {"example": example}
```

## Database tables and migrations

A plugin keeps its tables in its own migration branch, so the core migrations never change.

**Models.** Put the plugin's SQLAlchemy models in a `models` submodule of the plugin package
(`my_company.scribe_plugin.models` for `SCRIBE_PLUGINS=my_company.scribe_plugin`) and declare
them on the core declarative base, `scribe.db.base.Base` (also importable from
`scribe.db.models`), so they share `Base.metadata` with the core tables and can reference them
with foreign keys:

```python
import uuid

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from scribe.db.base import Base


class ExampleItem(Base):
    __tablename__ = "example_items"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
```

`alembic/env.py` reads `SCRIBE_PLUGINS` (from the environment, then `.env`, as the service
does; no other setting is needed) and imports `<module>.models` for each listed module that has
one. A plugin without a `models` submodule is skipped; a plugin that cannot be imported, or
whose `models` fails on import, stops alembic with an error. So `alembic check` and
`alembic revision --autogenerate` see the plugin's tables only while `SCRIBE_PLUGINS` lists the
plugin; without it, `alembic check` against a database with the plugin's tables reports them as
removed.

**Migrations.** Start the plugin's first migration as a new branch with its own label that
depends on the core revision it needs:

```python
revision = "example_0001"
down_revision = None
branch_labels = ("example",)
depends_on = "0023"  # the core revision whose tables this branch uses
```

Later plugin migrations chain on `down_revision` within the branch as usual. Alembic finds the
plugin's migration files through `version_locations`: copy the files into `alembic/versions/`
when you build your image, or list both directories in `alembic.ini`, separated by the
system path separator (`path_separator = os`; `:` on Linux), for example
`version_locations = %(here)s/alembic/versions:/path/to/plugin/migrations`.

The repository then has two heads, the core's and the plugin's, so always migrate with
`alembic upgrade heads` (plural; with no plugin it does the same as `upgrade head`).
`alembic upgrade head` refuses with "Multiple head revisions". Generate a plugin migration with
`alembic revision --autogenerate --head example@head -m "..."`.
