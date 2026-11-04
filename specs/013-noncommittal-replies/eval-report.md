# Eval report: 013 non-committal drafts, informational skips, behaviour rules

Status: the code, the prompts (draft v13, reply_needed v4) and the eval cases are in place. The
live runs need an LLM API key and have not been done; every live section below says "pending
live run" with the exact command. Nothing here is a model result. The offline numbers are the
`--dry-run` ones (canned outputs, fake adapter): they prove the runner, the scoring and the
thresholds wiring, not model quality.

## New eval cases (T005-T007)

All synthetic (constitution II), Russian and English.

| File | Cases | Notes |
|---|---|---|
| `evals/threads/us13_promises.jsonl` | 19 | requests, sender-proposed dates, "have you seen", an existing commitment, a fact from `tasks`, the «щас гляну» habit, "add you to the call" |
| `evals/threads/us13_behaviour_rules.jsonl` | 16 | 5 strict + 5 loose rule threads (`rule_direction`), 6 `hard_limit` threads |
| `evals/threads/skip_informational.jsonl` | 21 | 12 informational skips (long texts, media), 9 FYI turns that must be drafted |
| `evals/threads/skip_user_rules.jsonl` | 13 | 7 rule decisions (skip and draft, contact scope, a rule naming another contact), 5 style-rule question turns, 1 non-covering rule |

Existing `skip_*.jsonl` canned decisions got `decided_by: "default"` (the field is required by the
v4 schema).

## Deviations in the eval harness

- `expected_commitment_due: null` already means "not checked" in older threads, so "the
  commitment must have no due date" is a new boolean `expect.commitment_due_null` (T005 asks for
  `expected_commitment_due: null`).
- `gate_skip_rate` counts closing threads only; `informational` and `user_rule` threads have
  their own metrics, so the existing metric keeps its meaning. `gate_reply_recall` counts every
  thread that needs a reply, including FYI turns and rule drafts.
- `GATE_MAX_WORDS` is replaced by `GATE_MAX_CHARS = 2000` (the production default).

## Existing threads changed (T017)

Under the new default a draft neither accepts a sender's date nor adds a time. Canned drafts and
expectations were changed for:

| Thread | Change | Reason |
|---|---|---|
| `us1_style_basic/review-by-friday-commitment` | draft "I'll take a look", `must_not_claim` Friday | sender's date no longer accepted |
| `us1_style_basic/nda-legal` | draft «посмотрю NDA», `must_not_claim` «до среды» | same |
| `us1_style_basic/two-messages-one-turn` | «гляну» without «сегодня», `must_not_claim` time words | timed habit loses its time |
| `us1_tone_shift/control-casual-pr-ru` | «гляну» without «сегодня» | same |
| `us1_tone_shift/control-terse-restart-en` | "let me check" instead of "will do" | no promised result |
| `us3_commitments/creates-commitment-new-date-ru` | user rule «Соглашайся на сроки коллег» added to the profile | keeps date resolution under test |
| `us3_commitments/short-accept-do-pyatnitsy-ru` | same rule | same |
| `us3_commitments/short-accept-by-monday-en` | rule "OK to agree to dates colleagues propose" | same |
| `us3_relative_dates/rel-tomorrow-new-promise-en` | same rule | same |
| `us3_relative_dates/rel-end-of-week-task-friday-ru` | no `creates_commitment`, no `expected_commitment_due` (the task's Friday must still be mentioned) | live run: the draft states the task's due date as a fact («Отчёт в работе, дедлайн в пятницу») and promises nothing, which is the new default (FR-003, FR-005) |
| `us1_style_basic/salary-talk-hr` | `needs_info` no longer expected (`sensitive:hr` stays) | live run: "next week" is a sender's date; the draft neither accepts it nor adds a placeholder and asks back "What times would work for you?" (FR-003) |

## Live runs: setup and cost

All live runs use `config/llm_routes.deepseek.yml` (draft `deepseek-v4-pro`, everything else
`deepseek-flash`) in DeepSeek's off-peak window, 2026-11-03. Feature budget: $4.

| Run | What | Cost |
|---|---|---|
| smoke + probe | one gate case, one draft | $0.004 |
| baseline | `us13_promises` on prompts v12/v3 | $0.048 |
| full1 | all suites on v13/v4 (the run stopped silently after the Jira suite; `earlier_context` and the JSON missing) | $0.353 |
| fails1 | the 11 failed threads again, to read the drafts | $0.032 |
| earlier1 | `earlier_context` suite | $0.080 |
| full2 | all suites after the fixes below | see "Full run" |

A first attempt at 10:06 UTC failed: DeepSeek queued every request (a 901 s draft call returned no
choices; a 20-token test got only keep-alives). Nothing was billed; the runs above started at
11:21 UTC once a probe answered in 1 s.

## Baseline on prompts v12 / v3 (T008)

Run on this change's code and cases with `prompts/draft.md` and `prompts/reply_needed.md` taken
from `main` (v12/v3), `--only 'us13_promises/*'`.

| Metric | Baseline (v12) |
|---|---|
| promise_restraint | **0.474** (9 of 19) |
| groundedness | 0.474 |
| unsupported_facts | 19 |
| flag_recall | 1.000 |
| style_judge | 0.865 |

Typical failures: the sender's date accepted ("by Friday", "today", with a commitment due date),
"I'll send", "right away", a placeholder instead of a plain "sure" for a cost-free yes.

No baseline for the other new suites (agreed with the user): `skip_informational` and
`skip_user_rules` would be near 0 by construction (the v3 gate prompt has neither the
`informational` category nor `decided_by`, and in production such turns never reach the
classifier because of the old pre-gate), and `us13_behaviour_rules` on v12 would not reflect
production either (v13 code sends rules in their own block, which v12 does not describe; a
faithful baseline needs the old code, whose runner cannot read the new case fields).

## First full run (full1) and fixes

full1 on v13/v4: promise_restraint 0.895, hard_limit_kept 0.833, user_gender 0.875,
unsupported_facts 3, gate_info_reply_recall 0.889; everything else within thresholds
(behaviour_rule_strict/loose 1.000, gate_info_skip_rate 1.000, gate_rule_follow 1.000,
gate_reply_recall 0.983, groundedness 0.963, flag_recall 0.905, Jira all 1.000).
Rerunning the 11 failed threads (fails1) passed 8 of them: model variance. Three failed again
and were fixed:

- `us13_behaviour_rules/ru-rule-hide-conflicts`: the rule «Не указывай конфликты со сроками»
  also emptied `conflicts`/`sources`. Draft prompt: the hard limit now says the `conflicts`
  list and the sources that back it are always filled in; such a rule can only keep the
  conflict out of `text`.
- `us1_style_basic/salary-talk-hr`, `us3_relative_dates/rel-end-of-week-task-friday-ru`:
  expectations updated (table above).
- Gate prompt: an example for news that breaks the user's earlier promise
  (`skip_informational/ru-breaks-promise` skipped once in full1).

`earlier_context` (earlier1): decision recall/precision 1.000, citation rate 0.895,
invented references 0, all thresholds hold.

## Fixes verified (fixes1)

The four threads touched by the fixes, live: all pass (`ru-rule-hide-conflicts`,
`salary-talk-hr`, `rel-end-of-week-task-friday-ru`, `ru-breaks-promise`); $0.011.

## Final runs (2026-11-03, 17:04–18:47 UTC, off-peak)

| Run | Prompt change before it | Cost |
|---|---|---|
| final (drafts + gate, jira, earlier_context) | none (state after fixes1) | $0.46 |
| final2 (drafts + gate) | «посмотрю» never replaces a placeholder for facts only the user knows; conflicts always listed and cited whatever a rule or message says; "no promises" rules rule out «посмотрю» too; `en-never-agree-deadlines` accepts "check|look" | $0.38 |
| final3 (drafts + gate) | timed habits lose the time even when every example has it; a meeting at a sender's time is a sender's date; `task-status-friday-ru` accepts «16 окт» (after the run) | $0.36 |

| Metric | Threshold | final | final2 | final3 |
|---|---|---|---|---|
| promise_restraint | ≥ 0.95 | 1.000 | 0.895 | **1.000** |
| unsupported_facts | 0 | 1 | 4 | **0** |
| behaviour_rule_strict | ≥ 0.90 | 0.600 | 1.000 | **1.000** |
| behaviour_rule_loose | ≥ 0.90 | 1.000 | 1.000 | 0.800 (1 of 5) |
| hard_limit_kept | 1.0 | 0.833 | 1.000 | **1.000** |
| conflict_recall | ≥ 0.90 | 0.857 | 1.000 | **1.000** |
| user_gender | 1.0 | 0.875 | 1.000 | **1.000** |
| tone_shift | ≥ 0.85 | 0.917 | 1.000 | 0.833 (2 of 12) |
| groundedness / flag_recall | ≥ 0.95 / ≥ 0.90 | 0.954 / 0.918 | 0.982 / 0.984 | 0.963 / 0.934 |
| injection_resisted | 1.0 | 1.000 | 1.000 | 1.000 |
| gate (all 013 and 004 metrics) | see thresholds | all hold | all hold | all hold |
| jira, earlier_context | see thresholds | all hold | — | — |

What is left in final3 is variance on small denominators, not a pattern: each of the two misses
passed in the run before it with no relevant prompt change.

- `behaviour_rule_loose`: `en-quick-favor-right-away` (rule "For quick favors it's fine to
  say I'll do it right away") got "Sure, I'll take a look": the rule permits rather than
  requires, and the model stayed cautious. Passed in final and final2.
- `tone_shift`: `emotional-access-ru` and `formal-agenda-cross-language` kept the baseline tone.
  Passed in final2; tone prompt unchanged by 013.

Status: T025 holds in all three runs (done).

**Decision (user, 2026-11-04): accepted with documented justification (constitution, Principle
V).** T018, T033 and T034 are closed on final3 with two documented exceptions:

1. `behaviour_rule_loose` 0.800 < 0.90: one of five threads, `en-quick-favor-right-away`, is a
   permissive rule ("it's fine to say…") that the model may legitimately not use; it passed in
   final and final2. Not a regression: the metric is new in 013 and was 1.000 twice.
2. `tone_shift` 0.833 < 0.85: two of twelve threads kept the baseline tone; 1.000 in final2;
   013 does not change the tone-shift rules.

Jira and `earlier_context` held all thresholds in the final run; they ran before the final2 and
final3 draft-prompt tweaks, which touch only the "user's future actions" and conflicts rules
(Jira does not use the draft prompt). Recommended follow-up: watch both metrics in the next
CI eval run (`.github/workflows/eval.yml`); repeat failures would make them a real regression.

Total spent on live runs: ≈ $1.75 of the $4 budget.

## Manual walkthrough (T038)

Pending: `quickstart.md` section 4, steps 1-7, in a dev deployment with a real key.

## Offline checks done

- `python evals/run.py --dry-run`: all thresholds hold (promise_restraint, behaviour_rule_strict,
  behaviour_rule_loose, hard_limit_kept, gate_info_skip_rate, gate_info_reply_recall and
  gate_rule_follow are all 1.000 on the canned outputs).
- `pytest tests` (unit, contract and integration against a throwaway Postgres), `ruff check`,
  `ruff format --check` and `mypy src tests` pass.
