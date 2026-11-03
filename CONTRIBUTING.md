# Contributing to Scribe

Thanks for your interest! Bug reports, ideas and pull requests are all welcome.

Issues and pull requests are written in **English**. The bot itself speaks English and
Russian, so a change to user-facing text needs both (`src/scribe/i18n/en.py` and `ru.py`).

## Before you start

- **Bugs:** open an issue with the bug template. Never paste real messages, Telegram ids,
  tokens or session strings: replace them with made-up ones.
- **Features:** open an issue first and describe the problem, not only the solution. Bigger
  features get a spec under [`specs/`](specs/) before code.
- **Security problems:** do not open an issue, see [SECURITY.md](SECURITY.md).

The project rules are in the [constitution](.specify/memory/constitution.md). The most
important ones for contributors:

1. Nothing is ever sent on the user's behalf without their approval of that message.
2. Real message content never goes into the repository, logs or test fixtures. Fixtures are
   synthetic.
3. Drafts state only facts that are in the context; anything else becomes a `[[…]]`
   placeholder.
4. A change that affects draft generation comes with evaluation threads in [`evals/`](evals/).

## Development setup

You need Python 3.12, [uv](https://docs.astral.sh/uv/) and Docker (for Postgres and the
integration tests).

```bash
uv sync
make lint typecheck      # ruff + mypy (strict for src/scribe)
make test-fast           # unit + contract tests, no database, ~20 s
make test-integration    # starts Postgres through testcontainers
make eval-dry            # eval runner with the canned outputs, no API calls
```

To run the bot itself, follow [docs/self-hosting.md](docs/self-hosting.md). Set
`SCRIBE_DELAY_MIN_S=0`, `SCRIBE_DELAY_MAX_S=0` and `SCRIBE_TEST_FAST_CLOCK=true` in `.env` for
quick manual tests.

The code map is in [docs/architecture.md](docs/architecture.md).

## Making a change

- **Branch** from `main`; name it after the issue, e.g. `fix-47-forwarded-messages`.
- **Commits** follow [Conventional Commits](https://www.conventionalcommits.org/):
  `feat(scope): …`, `fix(scope): …`, `docs: …`, `test: …`, `refactor: …`, `ci: …`,
  `chore: …`. Scopes match the package: `intake`, `drafting`, `style`, `settings`, `jira`, …
- **Database changes** need an Alembic migration in `alembic/versions/` with the next number.
  Check there is a single head: `uv run alembic heads`.
- **Prompts** live in [`prompts/`](prompts/). If you change a prompt or an LLM route, add or
  update threads in `evals/threads/` and run `make eval-dry`. A real run (`make eval`) costs
  money and needs API keys; maintainers run it on request (Actions → "Evaluation set", which
  uploads the per-thread drafts and problems as the `eval-results` artifact). While iterating on
  a failure, `make eval ONLY="user_gender/*,*/conflict-*"` (or the workflow's `only` input) runs
  a subset; only the full set proves the thresholds. Each suite ends with its cost by route:
  calls, dollars, output tokens, the reasoning part of them and the largest single output
  (also under `cost_by_route` in the JSON), which is what to check before changing a route's
  `effort` or `max_tokens`.
- **User-visible changes** may deserve release notes: see
  [docs/release-notes.md](docs/release-notes.md). The maintainers bump the version.
- **Tests:** every bug fix comes with a test that fails without it.

## Pull requests

CI runs ruff, mypy, unit, contract and integration tests, gitleaks and pip-audit. All must
pass. In the description, say what changed and why, link the issue, and say how you tested
it. Keep pull requests focused: one issue per pull request.

## License of contributions

Scribe is distributed under the Apache License 2.0 with the Commons Clause (see
[LICENSE](LICENSE)). By opening a pull request you agree that your contribution is licensed
under the same terms.
