# Evaluation Report: Skip Messages That Need No Reply

**Feature**: [spec.md](spec.md) | **Task**: T037 | **Date**: 2026-10-21

**Status**: ⚠️ **INCOMPLETE. The live run is still pending.** This environment had no
`ANTHROPIC_API_KEY`, so `make eval` could not run against the real model. This report holds
only the offline dry run (`make eval-dry`), which proves the runner, the gate wiring, the
metric arithmetic and the thresholds. **The dry run says nothing about how the model
classifies.** T037 stays open until the live numbers below are filled in.

## Suite

`evals/threads/skip_*.jsonl`: 73 synthetic threads in ru and en (research R7; minimums from
T019).

| File | Threads | Content |
|---|---|---|
| `skip_closing.jsonl` | 34 | 30 clear closings (thanks, farewell, "ok"/"ок"/"👍" answering the user, "спасибо" with no earlier exchange, a group "thanks @me") + 4 `pending_draft` cases (2 with `resolves_pending=true`) |
| `skip_needs_reply.jsonl` | 32 | 27 that need a reply (thanks + question / request / new info, "ok?", "bye?", sarcasm, a greeting alone, "hi, do you have a minute?", complaints, disagreements) + 5 injection cases |
| `skip_ambiguous.jsonl` | 7 | ambiguous turns that must be drafted ("ok then", "ну ок", "fine.", "well...") |

Each thread goes through the real `scribe.drafting.reply_gate.decide()` (pre-gate +
`classify.reply_needed`). It runs with `enabled=True`, `draft_anyway=False`,
`habit_replied=no_habit`, `min_confidence=0.8` and `max_words=15`.

## Dry run (`make eval-dry`), 2026-10-21, after the port to `main`

The canned classifier output for each thread comes from its `dry_run.decision`, served by the
in-process fake adapter. Exit code 0: **all thresholds hold**, for the draft suite and the
gate suite.

| Gate metric | Dry-run value | Threshold |
|---|---|---|
| `gate_reply_recall` | 1.000 | ≥ 0.98 (`gate_reply_recall_min`) |
| `gate_skip_rate` | 1.000 | ≥ 0.85 (`gate_skip_rate_min`) |
| `gate_injection_forced_skip` | 0 | ≤ 0 (`gate_injection_forced_skip_max`) |
| `gate_resolves_pending_accuracy` | 1.000 | info |
| pre-gate candidate share | 0.877 (64 / 73) | info |
| classifier calls | 64 | |
| classifier latency p50 / p95 | 0 / 0 ms (fake adapter) | SC-005 budget: p95 ≤ 3 s |
| cost per `classify.reply_needed` call | $0.00009 (fake token counts at Haiku 5.5 prices) | |

Draft suite, unchanged by this feature (57 threads on `main`, the same numbers as before the
port):
- groundedness 1.000, unsupported_facts 0, flag_recall 1.000, conflict_recall 1.000;
- style_judge 0.898, tone_shift 1.000, injection_resisted 1.000; cost per draft $0.01076.

Cost by route in the dry run:

| Route | Calls | Total |
|---|---|---|
| `classify.reply_needed` (fake/claude-haiku-5-5) | 64 | $0.00570 |
| `compose.draft` (fake/claude-opus-5-5) | 57 | $0.61217 |
| `eval.style_judge` (fake/claude-haiku-5-5) | 57 | $0.00233 |
| `retrieve.filter_context` (fake/claude-haiku-5-5) | 25 | $0.00115 |

These numbers are self-consistent by construction: each canned decision matches its
expectation. The latency is zero because the fake adapter does not wait. The cost is computed
from character-count token estimates, not real usage.

**About the candidate share**: 0.877 describes this suite, not production traffic. The suite
is deliberately dominated by short messages, because those are the cases the classifier has to
judge. The plan's estimate of about one third of real turns reaching the classifier still has
to be checked against production intake.

## Live run: PENDING

Run with a real key:

```
ANTHROPIC_API_KEY=… make eval
```

Then fill in this table:

| Item | Value | Notes |
|---|---|---|
| `gate_reply_recall` | _pending_ | must be ≥ 0.98. If lower, raise `skip_min_confidence` or revise `prompts/reply_needed.md` (bump `version`) and re-run; do not lower the threshold |
| `gate_skip_rate` | _pending_ | must be ≥ 0.85 |
| `gate_injection_forced_skip` | _pending_ | must be 0 |
| `gate_resolves_pending_accuracy` | _pending_ | |
| per-call cost of `classify.reply_needed` | _pending_ | "cost per classifier call" in the report |
| `classify.reply_needed` latency p50 / p95 | _pending_ | the p95 must be ≤ 3 s (SC-005). If higher, lower `max_tokens` or pick a faster model before release |
| share of pre-gate candidates | 0.877 on this suite | see the note above |
| draft-suite metrics with the new route present | _pending_ | must still hold the existing thresholds |

## Scheduler throughput with the gate (offline, done)

001 T043 is on `main`, so the T037 variant exists:
`tests/integration/test_scheduler_throughput.py::test_scheduler_throughput_with_reply_gate`.
100 users × 20 turns, all due at once, half of them "thanks!" (pre-gate candidates the fake
classifier skips), 2 s per fake LLM call, time scaled ×50. It passes: every turn is read
within 60 s of `due_at` and exactly once, 1000 turns end `skipped` (1000 classifier calls,
no draft) and 1000 `pending_review` (no classifier call). As in 001 T043, the test asserts
reading lateness, not "final state within 60 s": drafting 1000 turns at
`compose_concurrency=10` with 2 s calls takes longer than that with or without the gate. This
uses the fake adapter, so it says nothing about the real classifier's latency; the SC-005
budget (p95 ≤ 3 s) still needs the live run above.
