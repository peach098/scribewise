# Quickstart: validating 013

Prerequisites: a dev checkout with the test database (`docker compose up -d db`, see the repo
README) and an `.env` with an LLM key for live eval runs. Contracts: [contracts/](./contracts/).

## 1. Baseline before the prompt change (SC-001)

Add the new eval suites first (research R8), then run them against prompts v12/v3:

```sh
python evals/run.py --only 'us13_*/*,skip_informational/*,skip_user_rules/*'
```

Record the numbers in `specs/013-noncommittal-replies/eval-report.md` as "baseline".

## 2. Automated checks

```sh
pytest tests/unit/test_reply_gate.py tests/unit/test_prompt_assembly.py \
  tests/contract/test_reply_needed_schema.py tests/contract/test_draft_output_schema.py
pytest tests/integration -k "skip or rules"
alembic upgrade head   # 0020 applies; enums gain informational, user_rule, rule
```

Expected: pre-gate admits long/media/FYI turns; a "?" turn is a candidate only with user rules;
`decide` maps `decided_by=user_rule` per the table in [contracts/reply-gate.md](./contracts/reply-gate.md);
prompt versions draft=13, reply_needed=4.

## 3. Full eval run (Principle V)

```sh
python evals/run.py            # live; add --dry-run for canned outputs
```

Expected (thresholds in `evals/thresholds.yml`): `promise_restraint ≥ 0.95`,
`behaviour_rule_strict ≥ 0.90`, `behaviour_rule_loose ≥ 0.90`, `hard_limit_kept = 1.0`, `gate_info_skip_rate ≥ 0.90`,
`gate_info_reply_recall ≥ 0.95`, `gate_rule_follow ≥ 0.90`; existing metrics within their
thresholds. Write results under "after" in `eval-report.md`.

## 4. Manual check in a dev deployment

1. Global rule «не обещай сроки, пиши "посмотрю"»; from a test contact send «Пришлёшь отчёт
   сегодня?» → draft reads like «Посмотрю, что по отчёту», no «сегодня», flagged as a commitment
   without a date.
2. Remove the rule; send «Глянь PR, пожалуйста» → «Гляну» (no time) by default.
3. Send «К сведению: релиз перенесли на четверг» → no card; `/skipped` shows it with
   «информационное сообщение»; "draft anyway" produces a draft.
4. Contact rule «всегда готовь черновик» for that contact; repeat step 3 → a draft appears.
5. Global rule «не отвечай на объявления»; send an announcement that mentions the user's task
   → skipped with «по вашему правилу».
6. Turn skipping off for the chat → every turn drafted, rule from step 5 notwithstanding.
7. Rule «отправляй без моего подтверждения» → nothing is sent; the card still waits for review.

## 5. Production metrics (SC-004/SC-005)

Two weeks after release compare, against the two weeks before: share of reviewed drafts whose
sent text removed a time/immediacy word, and share of drafts rejected unedited for messages now
classified `informational` (manual sample of 50 per period).
