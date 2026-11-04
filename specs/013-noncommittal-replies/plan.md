# Implementation Plan: Non-Committal Drafts, Informational Skips, Behaviour Rules

**Branch**: `013-noncommittal-replies` | **Date**: 2026-11-03 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/013-noncommittal-replies/spec.md`

## Summary

Drafts promise too much («сейчас сделаю», «пришлю сегодня»), informational messages get drafts,
and a user's rule against promises has no effect. Research ([research.md](./research.md) R1)
found why the rule fails: user rules sit in an untrusted data block, are ranked only among style
sources, lose to unconditional "agree / promise to look" lines in the draft prompt, and never
reach the reply gate, whose pre-gate drafts long, media or question turns without a model call.

Approach:

1. **Behaviour rules** — user rules move into their own `user_rules` block rendered
   `trust="user"`, sent to both the draft and the reply-needed prompts, with one shared "User
   rules" section: rules (style and behaviour) beat the default rules of the prompt, never the
   hard limits.
2. **Non-committal default** — draft prompt v13 replaces the promise paragraphs with "The user's
   future actions": «посмотрю»-style intention, no time/immediacy/result, a sender's date
   neither accepted nor placeholdered; «посмотрю» stays a commitment without a due date.
3. **Informational skips** — reply_needed prompt v4 adds category `informational` and
   `decided_by`; the pre-gate is widened (≤ 2000 chars, media allowed, "?" passes when the user
   has rules); `decide` maps rule decisions to source `rule` (skip category `user_rule`,
   bypassing the habit rule).
4. **Storage/UI** — migration `0020` adds enum values; `/skipped` shows the two new reasons;
   `/rules` help mentions behaviour rules.
5. **Evals** — four new suites, six new thresholds, baseline run before the prompt change.

## Technical Context

**Language/Version**: Python 3.12

**Primary Dependencies**: Telethon (user account), aiogram 3 (control bot), SQLAlchemy 2 async +
Alembic, pydantic 2 / pydantic-settings, LLM router (`src/scribe/llm`)

**Storage**: PostgreSQL; only enum values added (`skip_category`, `decision_source`)

**Testing**: pytest (unit, contract, integration), `evals/run.py` with `evals/thresholds.yml`
(CI workflow `.github/workflows/eval.yml`)

**Target Platform**: Linux server (docker compose deployment)

**Project Type**: single Python service (Telegram agent + control bot)

**Performance Goals**: no change to draft latency; extra classifier calls only for turns the
widened pre-gate admits (cheap route, a few hundred input tokens each); each correct
informational skip saves a draft call

**Constraints**: prompt-injection resistance (incoming content never becomes an instruction);
no message or rule text in logs; constitution Principle I approval step untouchable by rules

**Scale/Scope**: 2 prompts, 1 gate module, 1 composer call site, 1 migration, 2 i18n files,
4 eval suites; beta-scale user base

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Check | Status |
|---|---|---|
| I. Human approval | No send path touched. Rules cannot lift the approval step (hard limit in both prompts, FR-010); quickstart step 7 checks it. | Pass |
| II. Privacy & minimization | User rules (the user's own text) now also go to the classifier route: needed for FR-008a. Logs add only `decided_by`/`has_rules`. Eval fixtures synthetic. | Pass |
| III. Grounded replies | Strengthened: no invented times/results; hard limit "no fabrication" above rules; «посмотрю» still flagged as a commitment. | Pass |
| IV. Personal style | «посмотрю» in the user's own phrasing from `examples`; style never overrides correctness. | Pass |
| V. Evaluation-driven | Baseline before change, new suites and thresholds, existing thresholds kept (research R8). | Pass |
| VI. Simplicity & observability | No new service, table or setting beyond `skip_candidate_max_chars`; one classifier, one new output field. | Pass |
| Security: injection resistance | Only `user_rules` is `trust="user"`; written solely by the owner via the control chat; JSON-escaped; prompts state a rule never makes other content an instruction. | Pass |

**Post-design re-check (after Phase 1)**: unchanged — Pass. No violations, Complexity Tracking
not needed.

## Project Structure

### Documentation (this feature)

```text
specs/013-noncommittal-replies/
├── plan.md              # This file
├── research.md          # Phase 0
├── data-model.md        # Phase 1
├── quickstart.md        # Phase 1
├── contracts/
│   ├── prompts.md                        # draft v13, reply_needed v4, shared user_rules text
│   ├── reply-gate.md                     # pre-gate and decide() changes
│   ├── reply-needed-output.schema.json   # supersedes the 004 mirror
│   └── control-chat.md                   # /skipped reasons, /rules help
├── checklists/requirements.md
└── tasks.md             # Phase 2 (/speckit-tasks)
```

### Source Code (repository root)

```text
prompts/
├── draft.md                     # v13
└── reply_needed.md              # v4
alembic/versions/
└── 0020_behaviour_rules.py      # enum values
src/scribe/
├── config.py                    # skip_candidate_max_chars; drop skip_candidate_max_words
├── db/models.py                 # SkipCategory, DecisionSource values
├── llm/provider.py              # Block.trust, ReplyNeededRequest.user_rules,
│                                # DraftRequest.user_rules, ReplyNeededOutput
├── drafting/
│   ├── reply_gate.py            # pre-gate, decide(), rules loading, logging
│   └── composer.py              # pass user_rules as its own block
├── telegram/control_bot/
│   ├── render.py                # SKIP_CATEGORIES
│   └── profile_command.py       # /rules help line
└── i18n/{en,ru}.py              # skipped.reason.informational / .user_rule, rules help
evals/
├── run.py                       # GateThread.user_rules, GateExpect.decided_by, new metrics
├── thresholds.yml               # six new thresholds
└── threads/
    ├── skip_informational.jsonl
    ├── skip_user_rules.jsonl
    ├── us13_promises.jsonl
    └── us13_behaviour_rules.jsonl
tests/
├── unit/test_reply_gate.py, test_prompt_assembly.py
├── contract/test_reply_needed_schema.py   # points to the 013 schema
└── integration/                           # gate with rules, skipped list reasons
```

**Structure Decision**: existing single-project layout (`src/scribe`, `prompts`, `evals`,
`tests`, `alembic`); no new modules. Existing eval threads whose drafts contain a timed promise
or agree to a sender's date (e.g. `us3_commitments.jsonl`, `us3_relative_dates.jsonl`) are
updated to the new default or given a user rule that keeps the old behaviour, case by case,
with the reason in `eval-report.md`.

## Complexity Tracking

No constitution violations.
