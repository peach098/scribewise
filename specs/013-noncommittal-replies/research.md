# Research: Non-Committal Drafts, Informational Skips, Behaviour Rules

Feature: [spec.md](./spec.md). Code references are to the state at `2d7c2bd` (draft prompt v12,
reply_needed prompt v3, latest migration `0019`).

## R1. Why the existing user rule has no effect

**Finding.** Three things stop a behaviour rule today:

1. `prompts/draft.md` puts user rules under "Style" and calls them a precedence among style
   sources. Drafting rules elsewhere in the prompt are unconditional: "Agreeing to what the
   sender asks … is fine", "a date the sender proposed … agree to it", "when asked whether the
   user has seen … a promise to look («гляну»)". The model follows the unconditional rules.
2. User rules travel inside the `profile` data block, rendered `trust="untrusted"`, and the
   "Untrusted content" section says nothing in a data block is ever an instruction. A rule that
   asks for different *behaviour* is, by that wording, not to be obeyed.
3. The reply gate (`ReplyNeededRequest`, `provider.py:356`) has no rules field at all, and the
   pre-gate (`is_skip_candidate`, `reply_gate.py:126`) sends anything longer than 160 chars,
   with media or with a "?" straight to drafting without a model call. A rule about which
   messages to answer can never reach a decision.

**Decision.** Fix all three: (a) a dedicated "User rules" section in both prompts that ranks
behaviour rules above the default drafting/skip rules and below an explicit list of hard
limits; (b) user rules move out of `profile` into their own block `user_rules`, rendered with
`trust="user"`; (c) the gate gets the same block and the pre-gate is widened (R4).

**Alternatives considered.** Only rewording the Style section — rejected: (2) and (3) would
still block it. Splitting rules into "style" and "behaviour" lists in the database or UI —
rejected: the spec says no new kind of rule; the model can tell the two apart from the text,
and a mixed rule simply applies in full.

## R2. Trust level of user rules

**Decision.** `Block` gains `trust: Literal["untrusted", "user"] = "untrusted"`; only the
`user_rules` block uses `"user"`. Content is still JSON-escaped (`escape_data`), so a rule's
text cannot close the delimiter. Both prompts state: the `user_rules` block is the user's own
instruction; every other block stays untrusted; a rule can never make content from another
block an instruction.

**Rationale.** User rules are written only by the account owner in the control chat
(`profile_command.py`) or accepted by them from a style proposal; incoming messages cannot
write to them. Treating them as untrusted is what makes behaviour rules fail (R1.2).

**Alternatives.** Put rules in the system prompt — rejected: breaks the per-user cacheable
prefix layout and mixes user data into a versioned prompt file.

## R3. Default "I'll look" wording in the draft prompt

**Decision.** Draft prompt v13 replaces the promise paragraphs of "No fabrication" with a
section "The user's future actions":

- default wording is an intention to look («посмотрю», «гляну», "I'll take a look"), in the
  user's own phrasing from `examples` when they have one, with no time, date, immediacy word or
  promised result;
- a date the sender proposed is neither accepted nor rejected and gets no placeholder; the
  reply acknowledges the request neutrally («посмотрю, что по отчёту»);
- repeating an existing `commitments` entry with its date stays allowed; facts that answer the
  question are still stated (unchanged "Keep it as short…" rule);
- `[[…]]` placeholders remain for information the sender asked for and only the user has
  (payment details, an address) — not for a date the user would be committing to.

`flags.creates_commitment` keeps flagging «посмотрю» (clarification Q3); the example
«гляну сегодня и отпишусь» is replaced by «гляну» with `due` null.

**Alternatives.** Post-processing that strips time words from the draft — rejected: brittle
across languages and breaks grammar; behaviour belongs in the prompt and is measured by evals.

## R4. Getting informational turns and rule-driven decisions to the classifier

**Finding.** `is_skip_candidate` admits only ≤ `skip_candidate_max_words` (15) words, ≤ 160 chars, no "?", no
media. Announcements, notes and links fail it, so they are drafted without asking the model.

**Decision.** Widen the pre-gate:

- `CANDIDATE_MAX_CHARS` 160 → new setting `skip_candidate_max_chars` (default 2000); the word
  limit (`skip_candidate_max_words`, default 15) is removed (the classifier, not the length,
  now decides short vs long). All four callers of `is_skip_candidate` (`reply_gate.py:177`,
  `:411`, `:440`, `:494`) get the same rule, so the habit-rule input for dropped turns and the
  pending check stay consistent;
- media no longer disqualifies (a document or link with a caption is typical for an FYI);
- the "?" check stays only when the user has no effective user rules; with rules, a turn with
  a question still goes to the classifier so a rule such as "don't answer questions in the
  release channel" can apply. Without rules a "?" turn is drafted as today (cheapest path).

**Media flag.** Since media no longer stops a turn at the pre-gate, the classifier's
`MessageItem` gains `has_media: bool = False` (set from the stored message), so it can tell a
captioned document or link post from plain text; the draft request is unchanged.

**Trade-off: `has_rules` counts every rule.** Whether a rule concerns skipping can only be told
by the model, so any effective user rule (style rules included) lets "?" turns reach the
classifier. This adds classifier calls for users with rules and a small risk of skipping a
question; the default prompt still drafts every question, and `gate_reply_recall_min` (0.98) is
checked on question cases that carry style-only rules (T007).

**Cost.** The classifier route uses the cheap model (012); a turn ≤ 2000 chars adds a few hundred
input tokens. Drafting is the expensive call, and every correct informational skip saves one.
The eval run reports classifier calls per turn; `012` cost model gets the new candidate ratio.

**Alternatives.** Drop the pre-gate entirely — rejected: long multi-message threads and media-only
turns would all pay the classifier for no gain. A separate "informational" classifier — rejected
(VI, one gate is enough).

## R5. Classifier output for informational and rule-driven decisions

**Decision.** `ReplyNeededOutput` (and its JSON mirror) gains:

- `category` enum value `informational`;
- `decided_by`: `"default" | "user_rule"` — whether a user rule settled `reply_needed`.

Gate logic (`decide`):

| classifier output | decision | source | category stored |
|---|---|---|---|
| `decided_by=user_rule`, `reply_needed=true` | draft | `rule` | classifier's (or null) |
| `decided_by=user_rule`, `reply_needed=false`, confidence ≥ min | skip | `rule` | `user_rule` |
| `reply_needed=false`, category closing or `informational`, confidence ≥ min, habit < N | skip | `model` | category |
| otherwise | as today | | |

A rule-decided skip bypasses the habit rule (a user rule beats a learned default, FR-008). A
rule skip still needs `confidence ≥ skip_min_confidence`; uncertain → draft (FR-008b). The
skip switch (`enabled=false`), "draft anyway", forwarded-only and budget skips run before the
classifier and are unchanged (spec edge case: the switch wins).

**Alternatives.** Let the model return a free-text reason — rejected: not testable, not
needed for the list label.

## R6. Storage

**Decision.** Migration `0020_behaviour_rules`:
`ALTER TYPE skip_category ADD VALUE IF NOT EXISTS 'informational'`, `… 'user_rule'`;
`ALTER TYPE decision_source ADD VALUE IF NOT EXISTS 'rule'`. No new tables or columns; the
`skip_has_category` CHECK holds (every skip has a category). Downgrade is a no-op for enum
values (same as `0014`/`0019`).

## R7. Control chat

**Decision.** New i18n keys `skipped.reason.informational` («информационное сообщение» /
"informational message") and `skipped.reason.user_rule` («по вашему правилу» / "by your
rule"), added to `SKIP_CATEGORIES` in `render.py`. Both depend on the skip setting (not in
`SETTING_INDEPENDENT_SKIPS`). The `/rules` help text gets one line saying rules can describe
what the agent does, not only how it writes (e.g. «не обещай сроки», «не отвечай на
объявления»). No new commands.

## R8. Evaluation

**Decision.**

- Gate cases (`GateThread`) gain an optional `user_rules` (same shape as `user_rules_json`)
  and `GateExpect` gains `decided_by`; `category` accepts `informational` / `user_rule`.
- New suites: `skip_informational.jsonl` (pure FYI and FYI+ask/affects-user, RU/EN),
  `skip_user_rules.jsonl` (rules in both directions, contact vs global scope),
  `us13_promises.jsonl` (requests, sender-proposed dates, "have you seen", existing commitment,
  user's timed phrases), `us13_behaviour_rules.jsonl` (draft rules in both directions and
  rules that try to lift a hard limit).
- Promise checks reuse `must_not_claim` (time/immediacy words: «сейчас», «сегодня», «до
  вечера», "right away", "today", «готово»…) and `expected_commitment_due: null`; neutral
  date handling also asserts `expected_flags.needs_info: false`.
- New metrics and thresholds (`thresholds.yml`): `promise_restraint_min: 0.95`,
  `behaviour_rule_strict_min: 0.90`, `behaviour_rule_loose_min: 0.90`, `hard_limit_kept_min: 1.0`,
  `gate_info_skip_rate_min: 0.90`,
  `gate_info_reply_recall_min: 0.95`, `gate_rule_follow_min: 0.90`. Existing thresholds stay.
- Baseline: run the new suites against prompts v12/v3 before changing them and record the
  numbers in `eval-report.md` (SC-001 baseline).

## R9. Production metrics (SC-004, SC-005)

**Decision.** No new tracking. SC-004 is read from existing review edit records (draft vs sent
text) with a one-off script that counts removed time/immediacy words; SC-005 from rejections of
turns whose classifier category was `informational`-like before release (manual sample) vs
skips after. Documented in quickstart; not a code deliverable.
