# ScribeWise: LLM cost optimisation, analysis and plan

Date: 2026-10-31. Code read at `origin/main` (969cbc6). Paths below are relative to the repo root. `file:line` refers to `origin/main`.
Scope: every LLM call, prompt-cache behaviour on Anthropic and DeepSeek, a cost model, and a ranked list of levers.
Not covered here, because another agent owns it: switching the hosted routes to DeepSeek and fixing cached-token accounting in `openai_compat._usage`.

> **Status after #64 (merged 2026-10-31):** two findings below are already addressed.
> `config/llm_prices.yml` now has DeepSeek's peak list prices (TL;DR item 5, the price part).
> `config/llm_routes.deepseek.yml` sets `reasoning_effort: true`, so each route's effort is sent
> and the cheap routes run at `low` (part of item 1; turning thinking fully off per route is
> still open). `openai_compat` now counts `prompt_cache_hit_tokens` as cached input. The cost
> figures were computed before these changes; `cost_model.py` already uses the same prices.
> The `llm_call` log still has no `user_id`. Line references are to 969cbc6.

---

## 0. TL;DR

1. **On DeepSeek, output tokens cost more than input, and thinking makes up most of the output.** DeepSeek defaults to thinking mode. The DeepSeek route table never sends an effort level (`providers.openai_compat` has no `reasoning_effort: true`), and the adapter cannot send `thinking: {type: disabled}`. So every route thinks at the server's default depth, including classify, filter and extract. Turning thinking off or down per route is the biggest lever that leaves draft quality alone: about −40% of the DeepSeek bill.
2. **DeepSeek caching is automatic and already pays off without code changes.** It caches by prefix, on disk, has no write premium, keeps entries for hours to days, and bills a hit at 2–3% of a miss. The static system prompt with its schema, and the per-user style profile, already hit (about −30% versus no cache). Reordering prompt blocks adds only about 6% more, because input is the smaller half of the bill.
3. **"Cache warming" is not worth building on either provider** (§4). On DeepSeek nothing expires within the working day and there is no write premium to amortise. On Anthropic, putting a breakpoint after the system prompt and using the 1-hour TTL beats keep-alive pings, and pings cannot carry `output_config.format` with `max_tokens: 0` anyway.
4. **Anthropic deployment, cheapest change:** add a cache breakpoint at the end of the system prompt, so the 2.8k-token draft prompt is shared across all users, and use the 1-hour TTL. About −21% per user, no prompt change.
5. **Prerequisite:** DeepSeek prices are $0 in `config/llm_prices.yml:21-22`, so `cost_usd` reads 0 on the hosted deployment. The `llm_call` log has no `user_id`, so per-user spend cannot be computed today.

Modelled monthly cost per active user (40 incoming work messages/day, 22 working days; assumptions in §5):

| Scenario | $/user/month | $/draft (all calls ÷ pipelines) |
|---|---:|---:|
| (a) Anthropic, current routes (quality tier = Opus 5.5) | **26.8** | 0.053 |
| (a′) Anthropic + system breakpoint + 1h TTL | 21.2 | 0.042 |
| (a-econ) Anthropic, economy tier (Sonnet 5.5) | 14.5 | 0.029 |
| (b) DeepSeek, current routes, *no* cache hits | 9.2 | 0.018 |
| (b2) DeepSeek, current prompt order, automatic cache | 6.4 | 0.013 |
| (c) DeepSeek, proposed prompt order | 6.0 | 0.012 |
| (d) (c) + per-route thinking control | **3.7** | 0.007 |
| (d-flash) (d) with compose on `deepseek-flash` | 1.8 | 0.0035 |

The model's uncertainty is roughly ±40%: token counts are estimated from character counts and thinking lengths are guessed. Calibrate it from `llm_call` logs once DeepSeek prices are filled in (issue 1).

---

## 1. Call map

| Route | Fires when | Caller | System prompt (≈tok) | User blocks, **in send order** | Data ≈tok | Out (visible + thinking) ≈tok | Anthropic route / DeepSeek route | Cache today |
|---|---|---|---|---|---|---|---|---|
| `classify.reply_needed` | Once per due turn, **only if the pre-gate passes** (≤15 words, no `?`, no media; `reply_gate.py:118`). Also once per new message while a draft is pending (`decide_for_pending`) | `drafting/reply_gate.py:171` | `reply_needed.md` 850 (+380 schema on DS) | incoming_messages → history (≤8, `config.py:82`) → contact → pending_draft | ~630 | 120 + think | Haiku 5.5 low, 500 / flash, 8000, no effort | none (Anth.); DS automatic: system+schema |
| `retrieve.filter_context` | **Every draft pipeline**: each turn that is drafted, each regenerate. Forced even with no candidates when search is on (`composer.py:356-361`) | `composer.py:367` | `filter_context.md` 760 (+330) | **incoming_messages → candidates{history ≤60, tasks ≤100, commitments}** (`provider.py:288-290`) | ~6,400 (60 history × ~64 + 30 tasks × ~78) | ~435 (15 ids × ~25 tok for `msg:<uuid>`) + think | Haiku low, 2000 / flash, 8000 | none; DS: system only (measured common prefix between consecutive turns: **63 chars of 13,236**) |
| `retrieve.filter_context` (rank_earlier) | When the filter asks for earlier context and search finds matches (assumed ~15% of drafts) | `composer.py:451` | same | incoming → candidates.history (matches) | ~1,400 | ~300 + think | same | same |
| `extract.deadlines` | Per draft, only for selected tasks whose comments match a deadline hint and are newer than the parsed ones; ≤10 tasks (`context/tasks/deadlines.py:324-350, 402-428`) | `composer.py:613` → `deadlines.refine_deadlines` | `deadlines.md` 640 (+360) | timezone → tasks[texts] | ~1,000 | ~150 + think | Haiku low, 1000 / flash, 8000 | none; recomputed on every draft and regenerate for the same comments |
| `extract.commitments` | **Every outgoing message** in an enabled work chat, manual or agent-sent; only empty text is skipped (`context/commitments.py:550`). Also every new or edited user Jira comment (`comment_commitments.py:371`) | `commitments.py:612`, `sending/send.py:196`, `intake/events.py:174` | `commitments.md` 960 (+300) | outgoing_text → contact → sent_at → recent_incoming(≤5) [→ source, issue for Jira] | ~420 | ~120 + think | Haiku low, 2000 / flash, 8000 | none |
| `compose.draft` | Every drafted turn, every regenerate, plus a second call when sources are invalid (`composer.py:528-534`) | `composer.py:528` | `draft.md` **2,810** (+1,200 schema on DS) | **profile (stable)** → incoming_messages → history (filter-selected ≤15 + earlier ≤10) → contact → user → chat_overrides → tasks → commitments → user_state → today → timezone (`provider.py:216-220, 332-348`) | profile ~900 (50 examples global / 20 contact) + rest ~1,500 | ~400 + think (Opus 5.5 medium ≈1,000 assumed) | **Opus 5.5** medium 4000 (quality), Sonnet 5.5 (economy) / **v4-pro** (quality), flash (economy), 16000 | Anth.: one `cache_control` after profile, 5-min TTL (`anthropic.py:150-160`). DS: automatic, system+schema+profile |
| `profile.build` | Setup/import and 🔄 Rebuild (interactive, synchronous); weekly refresh Sunday 02:00 UTC (`jobs.py:47`) per profile with ≥10 new messages (`profiles.py:109`); retry once at lower effort with half the samples on truncation | `style/profiles.py:617, 686` | `style_profile.md` 500 (+540) | samples (≤400, `config.py:72`) → scope → existing_user_rules_json | global ~20,000; contact ~7,500 | ~1,500 + think (high) | **Opus 5.5 high, 16000, prefer_batch** / v4-pro high, 32000 | Anth.: **Message Batches (50%)** for the weekly job (`registry.py:258-307`, `anthropic.py:208-249`), no caching. DS: no batch API, so sequential calls (`openai_compat.py:189-209`) |
| `eval.style_judge` | Eval runs only (`evals/run.py`) | evals | `style_judge.md` ~320 | draft_text → profile_summary → reference_examples | small | small | Haiku / flash | none |

The router and adapters work like this:

- `LLMRouter._call` (`registry.py:342-376`) passes `cache_prefix` only from `draft_reply` (`registry.py:244`). Every other route sends `cache_prefix=False`.
- The OpenAI-compatible adapter joins all blocks into one user message (`openai_compat.py:143`). It ignores `cache_prefix` (`capabilities.prompt_caching=False`, `:82-87`), which is harmless because DeepSeek caches automatically. It sends `reasoning_effort` only when the provider flag is on (`:107`). The DeepSeek table does not set the flag (`config/llm_routes.deepseek.yml`, last line).
- `schema_fallback.py:67` appends the JSON schema to the system prompt. That is byte-stable and good for prefix caching. A corrective retry re-sends the same prefix, which also hits.
- Prompts are static files with no templating (checked: no `{` placeholders). `PromptStore` caches them (`registry.py:116-132`). Data blocks use `json.dumps(sort_keys=True)` (`provider.py:126-130`). No `now()`, random order or request ids appear before the data. **The system prompt and the profile are byte-stable**, which is good.

---

## 2. Findings (file:line)

**F1. DeepSeek: thinking is never controlled.**
- `openai_compat.py:107` sends `reasoning_effort` only when `providers.openai_compat.reasoning_effort` is true. `llm_routes.deepseek.yml` does not set it, so every route, including `classify` and `filter` marked `effort: low`, runs at DeepSeek's default (thinking on; source: DeepSeek thinking-mode guide).
- DeepSeek accepts only `low|high|max`; `medium` maps to `high`. So `effort: medium` on compose would mean `high`.
- Turning thinking fully off needs `extra_body={"thinking": {"type": "disabled"}}`, which the adapter cannot send.
- `max_tokens: 8000` on the flash routes leaves room for long reasoning on one-line classifications.

**F2. DeepSeek prices are zero in `config/llm_prices.yml:19-22`.** `cost_usd` for every DeepSeek call is $0. There is also no peak/off-peak model in `pricing.py` (one rate card per model, `pricing.py:20-26`).

**F3. Cost telemetry is incomplete per user.**
- The `llm_call` line (`registry.py:392-411`) has no `user_id`.
- `drafts.llm_usage_json` (`composer.py:296-315`) covers filter, rank and compose but **not `extract.deadlines`** (`composer.py:265-269`).
- Classify cost appears only in the `reply_gate_decided` log (`reply_gate.py:540`). Commitments extraction cost is not stored anywhere.
- There are no per-user budgets or quotas anywhere in the codebase.

**F4. Filter prompt order defeats prefix caching.**
- `FilterContextRequest` declares `incoming_messages` before `candidates` (`provider.py:288-290`), and `to_blocks` follows declaration order (`provider.py:216-220`). The volatile new message therefore sits in front of the largest, mostly repeating block: 60 history items plus up to 100 tasks (`composer.py:351`).
- Measured with the real serializer on two consecutive turns of one chat: the common prefix is **63 characters** (the `<data name="incoming_messages"` tag) out of 13,236.

**F5. Draft prompt order is good up to the profile, then volatile.** The Anthropic adapter (`anthropic.py:150-158`) and `to_blocks` put `profile` (the only `STABLE_FIELDS`, `provider.py:333`) first. The next block is `incoming_messages`, followed by blocks that are stable per chat (`contact`, `user`, `chat_overrides`, `user_state`, `timezone`) and per day (`today`). Measured common prefix between consecutive drafts in one chat: 2,452 of 5,610 characters, which is exactly the profile.

**F6. The history window slides and the filter subsets it.**
- `history.py:119-124` takes the 40 newest chat messages, and `:126-137` adds 20 more by the same contact from other chats, merged by time (`:148`). Each new message drops the oldest one, so the head of the history block changes on every turn.
- The draft does not receive that list. It receives the filter's per-turn subset (`composer.py:381-385`), so the draft's history is not cacheable across turns whatever the order.

**F7. Anthropic: one cache breakpoint, placed after the profile.**
- `anthropic.py:156-160` marks only the last stable block, with the default 5-minute TTL (`EPHEMERAL`, `:46`).
- The system prompt (2,810 tokens, identical for **all** users) gets no breakpoint of its own. A read therefore happens only when the same user and the same profile (global, or one of up to 20 contact profiles) drafted within 5 minutes.
- With drafts 2–10 minutes apart and ~23 drafts/user/day, I assume ~30% hits. Every miss pays a 1.25× write on ~3.7k tokens of Opus.
- The 512-token minimum for Opus/Sonnet/Haiku 5.5 is met (prefix ≈3.7k).
- `pricing.py:13` hardcodes the 1.25× 5-minute write price, so 1-hour writes (2×) would be mispriced.

**F8. Regenerate reruns the whole pipeline, with no cap.** `on_regen` (`telegram/control_bot/review.py:1087-1124`) calls `Composer.compose` again: filter, earlier search, rank, deadlines and compose. Given the same turn, the inputs are byte-identical. That is a 100% hit on DeepSeek, but on Anthropic only system plus profile is cached. The filter's result could be reused as is.

**F9. `extract.commitments` has no pre-filter.**
- It runs for every non-empty outgoing message (`commitments.py:550-552`), including "ок", "спасибо", "+".
- A recall-oriented keyword detector already exists (`drafting/promises.py:138 detect_promise`; future first-person verbs, "I'll", deadline acceptances), but it is applied only to drafts.
- For agent sends, the approved draft already carries `flags.creates_commitment` (what/due), and the extraction is repeated anyway (`sending/send.py:196`).

**F10. `extract.deadlines` is already gated** (hint regex, newer-than-parsed, ≤10 tasks). It is not memoised, so the same comment is re-asked on each draft and regenerate that selects the task. The cost is small.

**F11. The reply gate already avoids most classifier calls** (pre-gate `reply_gate.py:118`, no model call for forwards-only turns). Nothing to gain there.

**F12. Ids are long.** Every message id is `msg:<uuid>`, about 25 tokens. A filter answer of 15 ids is ~375 output tokens. In the draft, `sources[].id` repeats the ids. Ids are ~26% of each history item's characters, measured. Short per-request aliases (`h1`, `t3`, `c2`), mapped back in the router, would cut filter output by ~70% and history input by ~25%.

**F13. Batches and off-peak timing.**
- Anthropic: `profile.build` uses Message Batches (50%) for the weekly refresh only. Interactive builds are synchronous by design. Batch requests carry `cache_prefix=False` (`StructuredCall` default) and no fallbacks (`anthropic.py:175`). That is fine: profiles share only the 500-token system prompt.
- DeepSeek: there is no batch API. The official docs navigation and the 2026 changelog list none; third-party pages claiming one are unverified SEO content. The weekly job runs Sunday 02:00 UTC (`jobs.py:47`), which is **already off-peak**: DeepSeek peak is Mon–Fri 01:00–04:00 and 06:00–10:00 UTC, so weekends are off-peak.

---

## 3. Provider facts (primary sources, checked 2026-10-31)

**DeepSeek** (api-docs.deepseek.com: pricing, KV cache guide, thinking-mode guide, change log)

- **Prices per 1M tokens, off-peak / peak:**

  | Model | Input, cache hit | Input, cache miss | Output |
  |---|---:|---:|---:|
  | `deepseek-flash` (V4.1-Flash-1017) | $0.003 / $0.006 | $0.15 / $0.30 | $0.60 / $1.20 |
  | `deepseek-v4-pro` | $0.022 / $0.044 | $0.66 / $1.32 | $1.98 / $3.96 |

- **Peak and off-peak:** peak is Mon–Fri 01:00–04:00 and 06:00–10:00 UTC, excluding Chinese holidays. Off-peak is half price. This was introduced 2026-08-16; prices were cut again on 2026-09-10.
- **Context caching:**
  - On by default, disk-based, prefix-based, best-effort.
  - A hit requires a **full match of a persisted "cache prefix unit"**. Units are persisted (1) at the end of each request's user input and at the end of its output, (2) for a common prefix detected across requests (the docs example: the first two requests sharing system+document miss, the third hits), and (3) at fixed token intervals for long inputs.
  - Construction takes seconds. Unused entries are evicted "within a few hours to a few days".
  - Hits are billed at the cache-hit rate, with no write premium.
  - Usage fields: `prompt_cache_hit_tokens` and `prompt_cache_miss_tokens`.
  - Not stated in the docs: minimum length, unit size, interval size, whether caches are per account or per key. I could not verify a "64-token granularity"; the current page does not state one.
- **Thinking:** on by default on both models. `thinking: {type: enabled|disabled}` goes in `extra_body` with the OpenAI SDK. `reasoning_effort` accepts `low|high|max`; `medium` and `xhigh` map to `high`. **Unverified:** the docs do not say explicitly that reasoning tokens are billed as output. That is standard and consistent with `completion_tokens` including reasoning, but it should be confirmed against one real invoice line.
- **Interfaces:** DeepSeek also serves an Anthropic-compatible API and the OpenAI Responses API (change log 2026-04-24 and 2026-08-13). That is relevant to the route-switching agent, not to this plan.

**Anthropic** (claude-api skill reference cached 2026-10-06; `config/llm_prices.yml` agrees)

- **Prices per 1M tokens (input / output):**
  - Opus 5.5: $4 / $20, cache read $0.20 (0.05×).
  - Sonnet 5.5: $2 / $10, cache read $0.20.
  - Haiku 5.5: $0.10 / $0.50, cache read $0.01, for prompts up to 100k tokens.
- **Caching:**
  - Cache writes cost 1.25× input for the 5-minute TTL and **2×** for the 1-hour TTL.
  - Reads refresh the timer for free. The TTL counts from request start.
  - At most 4 breakpoints. 1-hour entries must come before 5-minute ones.
  - Minimum cacheable prefix is **512 tokens** on Opus/Sonnet/Haiku 5.5.
  - Caches are model-scoped and workspace-scoped. Changing `effort` or thinking settings invalidates the messages tier.
- **Pre-warming with `max_tokens: 0`** is rejected together with `output_config.format` or inside Batches. Every Scribe call uses structured output, so a true zero-token warm-up is impossible; a warm-up would have to be a real call.
- **Message Batches:** 50% off, usually under 1 hour, at most 24 hours. Caching works inside a batch but hits are best-effort. Server-side `fallbacks` are rejected in batches, as the adapter already handles.

---

## 4. Cache warming: verdict

**Short answer: do not build cache warming.** Make the prefix stable instead, and on Anthropic add one breakpoint and switch to the 1-hour TTL.

- **DeepSeek.**
  - Nothing to keep warm: entries live hours to days, far longer than the 2–10 minute draft delays or a working day. Hits need no write premium.
  - What a "warm" request *could* do is create a prefix unit early. A request whose input ends exactly at system+schema+profile persists a unit at that boundary, so the first real draft hits instead of waiting for the common prefix to be detected (the docs example shows two misses first).
  - That saves at most ~1 miss of a ~900-token profile per profile change (weekly) per user: ≈$0.001 on v4-pro. The warm-up itself costs about one miss.
  - The system prompt plus schema (~4k tokens on compose) is shared by **all** users of the hosted deployment, so it stays hot through real traffic within minutes of a deploy.
  - Net: zero or negative. Skip it.
- **Anthropic.**
  - The 5-minute TTL against a per-(user, profile) gap of ~26 minutes on average (bursty) means most compose calls miss today.
  - A keep-alive every ~4.5 minutes during a 10-hour day is ~133 calls per profile. Each would be a real structured call, since `max_tokens: 0` and `output_config.format` cannot be combined: ~$0.10/day/profile in reads plus thinking output. Contact profiles multiply that, so it is worse than the alternative.
  - The alternative is two explicit breakpoints:
    - (1) At the end of the system prompt. It is shared across users, so with ≥1 draft per 5 minutes anywhere in the deployment it stays warm without pings.
    - (2) After the profile, with `ttl: "1h"`. Use 1h on breakpoint (1) as well, because 1-hour entries must come first.
  - Modelled: about −$5.6 per user per month (−21%, scenario a′). This needs no prompt change, so no draft-quality risk, though the eval still has to be run because the request shape changes.
- **Where warming could matter:** first-draft latency right after a deploy or prompt-version bump on Anthropic. That is a latency concern, not a cost one. If wanted, send one real draft-shaped request per tier at startup.

---

## 5. Cost model

**Traffic per active user per working day (assumed):**
- 40 incoming work messages → ~25 turns. The grouping window is 60 seconds, same sender.
- Pre-gate candidates: ~30%, giving ~10 classify calls/day including the pending path. ~4 turns are skipped, leaving **21 drafts**.
- Regenerate on 10% of drafts (+2.1 pipelines), for **23.1 pipelines/day**. Invalid-source retry on 3% gives 23.8 compose calls/day.
- rank_earlier on 15% of pipelines (3.5/day). extract.deadlines on 10% (2.3/day, only with a tracker connected).
- 25 outgoing messages plus 2 Jira comments → 27 extract.commitments/day.
- 22 working days a month. Weekly refresh: 1 global profile (20k input tokens) plus 3 contact profiles (7.5k each).

**Token sizes:**
- System prompts: characters ÷ 4.
- DeepSeek adds the schema-in-system, measured with `schema_instructions()`: compose 3,894 characters ≈1.2k tokens.
- Data blocks: measured characters from real `to_blocks()` output on a synthetic Russian chat ÷ 2.7. JSON with Cyrillic text and UUIDs runs at about 2.7 characters per token.
- Thinking tokens assumed:

  | Setting | classify / filter / rank / deadlines / commitments | compose | profile |
  |---|---|---|---|
  | Anthropic (Haiku low, Opus medium, Opus high) | 100–200 | 1,000 | 4,000 |
  | DeepSeek default thinking | ~800–1,000 | 1,800 | 6,000 |
  | DeepSeek tuned (thinking off on the small routes) | 0–150 | 900 (`low`) | — |

**Caching assumptions:**

| Scenario | Assumption |
|---|---|
| (a) | 30% hits on the sys+profile prefix |
| (a′) | System prefix hits 98%, profile prefix 80%, 1-hour writes at 2× |
| (b2) | System+schema always hit. Profile hits 90%. Regenerates and retries hit fully |
| (c) | (b2) plus ~50% of filter candidate tokens hit on follow-up turns. That relies on DeepSeek's common-prefix and fixed-interval units, so it is optimistic until measured. Plus a small gain on draft fields that are static per chat |

**DeepSeek blended price:** for users on Moscow time working 9–19 local (06–16 UTC), 40% of calls fall in peak, so the blended factor is 0.7 × peak. The weekly job runs at off-peak (0.5).

**Breakdown per user per month, by route:**

| Scenario | classify | filter | rank | deadlines | commitments | compose | profile | **total** |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| (a) Anthropic current | 0.06 | 0.53 | 0.03 | 0.02 | 0.15 | **24.72** | 1.34 | **26.84** |
| (a′) + system/1h breakpoints | 0.06 | 0.49 | 0.03 | 0.02 | 0.15 | 19.16 | 1.34 | 21.24 |
| (b) DeepSeek, no cache | 0.26 | 1.41 | 0.11 | 0.06 | 0.67 | 6.29 | 0.39 | 9.19 |
| (b2) DeepSeek, auto cache | 0.20 | 1.24 | 0.09 | 0.05 | 0.52 | 3.96 | 0.38 | 6.43 |
| (c) + proposed order | 0.20 | 0.91 | 0.09 | 0.05 | 0.52 | 3.89 | 0.38 | 6.04 |
| (d) + thinking tuned | 0.05 | 0.48 | 0.04 | 0.02 | 0.17 | 2.59 | 0.31 | 3.66 |
| (d-flash) compose on flash | 0.05 | 0.48 | 0.04 | 0.02 | 0.17 | 0.72 | 0.31 | 1.80 |

What the breakdown shows:
- **compose.draft is 80–92% of spend in every scenario.**
- On v4-pro, a compose call in (b2) costs ≈$0.0076, of which ≈$0.0061 is output (≈2.2k tokens × $3.96 × 0.7). That is why thinking control beats caching on DeepSeek.
- On Anthropic, Opus output (1.4k × $20) and cache writes dominate.
- The small routes cost little on Haiku but are **relatively expensive on DeepSeek flash**: flash input is 3× Haiku's price, flash output 2.4× Haiku's, and thinking runs by default.
- Per 100 active users, the hosted DeepSeek bill drops from ≈$640/month (b2) to ≈$370/month (d).

The model lives in `scratchpad/specs/012-llm-cost-optimization/cost_model.py` (re-runnable; edit its constants).

---

## 6. Ranked proposals

Savings are given against (b2), the DeepSeek hosted deployment as it is about to ship, unless marked Anthropic. "Eval" means the full thread set must pass `evals/thresholds.yml` before shipping (Constitution Principle V: any change affecting draft generation, including prompts, models, routes, composer and style). The reply-gate, jira and earlier-context suites apply when their routes change.

| # | Proposal | Saving | Effort | Quality risk / eval |
|---|---|---|---|---|
| 1 | **Fill DeepSeek prices with peak/off-peak, and add per-user cost telemetry.** Rate card by time window in `pricing.py`. Add `user_id` to `llm_call`. Persist deadlines, classify and commitments cost per user, e.g. a daily `llm_spend` aggregate. Log `prompt_cache_hit/miss_tokens`. | 0 directly; needed to measure everything else | S–M | none |
| 2 | **Per-route thinking control on DeepSeek.** Adapter support for `thinking: {type: disabled}` via `extra_body`, plus `reasoning_effort: true` on the provider. Disable thinking for classify, filter and rank. Use `low` for extract.* and `low` for compose (DeepSeek has no `medium`; `medium` means `high`). Lower `max_tokens` on the small routes (8000 → 1000–2000). | **−$2.4/user/mo (−37%)** | S | Medium for compose and the gate (run the reply-gate and earlier-context suites); low for extract.* (run the jira suite) |
| 3 | **Anthropic: breakpoint at the end of the system prompt, plus 1h TTL on both breakpoints.** Price 1-hour writes at 2× (`pricing.py:13`, `usage.cache_creation.ephemeral_1h_input_tokens`). Optionally cache the filter system prompt as well. | **−$5.6/user/mo (−21%) on Anthropic** | S | none to the prompt text; run the eval (request shape changes) |
| 4 | **Model tiering for compose.** Make economy the default tier for new hosted users (flash on DeepSeek, Sonnet on Anthropic), or route "simple" turns (short incoming, no tasks or commitments selected, no earlier search) to the economy model and keep v4-pro/Opus for the rest. | DS: up to −$1.9 (−50% of (d)); Anthropic: −$12 (−46%) with Sonnet | M | **High.** Style judge and groundedness thresholds decide; eval per tier |
| 5 | **Short per-request ids** (`h1…`, `t1…`, `c1…`) in filter, rank and compose, mapped back in the router before `sanitize_filter_output` and `invalid_source_ids`. | ≈−$0.5/user/mo on DS (−8%); ≈−$1.7 on Anthropic | M | Low–medium: source validation must map aliases back; run the eval (groundedness) |
| 6 | **Reorder blocks for prefix stability.** `FilterContextRequest`: candidates (tasks → commitments → history) first, `incoming_messages` last. `DraftRequest`: profile → user → contact → chat_overrides → user_state → timezone → commitments → tasks → history → today → incoming last. Anchor the history window, e.g. keep 40–60 messages and move the start in steps of 20, so consecutive turns share a byte-identical head. | ≈−$0.4/user/mo (−6%); more in busy chats | S (order) / M (anchoring) | **Medium.** Instruction position changes attention ("incoming last" is usually fine); run the full eval |
| 7 | **Pre-filter `extract.commitments`.** Skip the LLM when a loose detector (a recall-oriented variant of `promises.detect_promise`, plus date words, plus length > N words) finds nothing. For agent sends of an unchanged approved draft, take `flags.creates_commitment` instead of re-extracting. | ≈−$0.25/user/mo DS (b2); 50–60% fewer calls | S–M | Medium on recall: run the us3 and jira commitment suites; add short "ок, до пятницы" cases |
| 8 | **Reuse filter, rank and deadline results on regenerate** when `turn.last_message_id` is unchanged (store selected ids in the draft's `llm_usage_json` or a new column). Memoise `extract.deadlines` per (task id, comment ids, timezone) for the day. | ≈−$0.1–0.15/user/mo; lower regenerate latency | S | Low: identical inputs today |
| 9 | **Regenerate cap and per-user budget.** For example, at most 3 regenerates per draft. A soft daily spend cap per user (from #1) downgrades to the economy tier and then pauses drafting with a notice. | Bounds the worst case (abuse, loops); typical saving ≈3% | M | Product decision; no prompt change |
| 10 | **Trim the filter input.** Cap tasks offered to the filter (`MAX_TASK_CANDIDATES` 100 → ~30, pre-ranked by contact, due date and keyword overlap). Drop `sent_at` seconds and offsets where the timezone is in the request. | ≈−$0.2/user/mo DS; less latency | S | Medium: task recall → conflict_recall; run the eval |
| 11 | **Keep the weekly refresh off-peak** (already Sunday 02:00 UTC). Document the constraint in `jobs.py`. If more jobs appear (Jira first scan, re-imports), schedule them outside 01–04 and 06–10 UTC on weekdays. DeepSeek has no batch API, so the 50% comes only from off-peak timing. | already captured (≈−50% on profile.build) | XS | none |

Not recommended:
- **Sharing a prefix between classify and compose:** different system prompts come first, and classify is cheap.
- **Anthropic auto-caching of the whole draft tail for regenerates:** an extra 0.25× write on ~1.5k tokens for every draft outweighs the hit on ~10% regenerates.
- **Cache warming:** see §4.

---

## 7. Proposed GitHub issues

**1. `cost: price DeepSeek models (peak/off-peak) and attribute LLM spend per user`**
> `config/llm_prices.yml` lists `deepseek-flash`/`deepseek-v4-pro` at $0, so every `llm_call` on the hosted deployment reports `cost_usd: 0`. Add the official rates (hit/miss/output) with DeepSeek's peak window (Mon–Fri 01–04 and 06–10 UTC; off-peak = 50%) to `PriceTable.cost_usd`, and price Anthropic 1h cache writes at 2×.
> Add `user_id` to the `llm_call` line and persist per-user daily spend across all routes (classify, deadlines and commitments are missing from `drafts.llm_usage_json` today).
> Log `prompt_cache_hit_tokens` and `prompt_cache_miss_tokens` (coordinate with the cached-token accounting fix).
> Acceptance: an admin query returns $/user/day split by route.

**2. `llm: per-route thinking control for openai_compat (DeepSeek)`**
> DeepSeek thinks by default, and `llm_routes.deepseek.yml` never sends effort (no `reasoning_effort: true`). So classify, filter and extract all reason at the server default, and output tokens dominate the bill.
> Add a route field (e.g. `thinking: off|low|high`) that the adapter maps to `extra_body.thinking` and `reasoning_effort` (DeepSeek accepts low/high/max; medium → high).
> Proposed: off for classify/filter, low for extract.* and compose, then lower `max_tokens` on the small routes.
> Ship only with the full eval plus the reply-gate, jira and earlier-context suites passing.

**3. `llm(anthropic): cache the shared system prompt and use the 1h TTL`**
> `AnthropicAdapter.build_params` places one 5-minute breakpoint after the style profile. The 2.8k-token draft system prompt is therefore cached only per (user, profile) within 5 minutes, and most drafts pay a 1.25× write.
> Add a breakpoint at the end of the system block, shared across users, and use `ttl: "1h"` on both breakpoints (1h must precede 5m). Optionally do the same for the filter prompt.
> Expected ≈−20% of Anthropic draft cost. Verify via `cache_read_input_tokens`; run the eval.

**4. `drafting: order prompt blocks for prefix caching (volatile data last)`**
> `FilterContextRequest` sends `incoming_messages` before the 60-message and 100-task candidate list. Two consecutive turns share only 63 characters of prefix.
> Reorder to candidates first and incoming last. In `DraftRequest`, move the per-chat static fields right after `profile` and put `incoming_messages` and `today` last. Anchor the history window so its head stays byte-identical across turns.
> Measure hit tokens on DeepSeek before and after. Prompt-visible change: full eval required.

**5. `llm: short per-request ids instead of msg:<uuid> in prompts`**
> Each `msg:<uuid>` costs ~25 tokens. Filter answers are mostly ids (~375 output tokens), and drafts repeat them in `sources`.
> Map ids to `h1…`, `t1…`, `c1…` per request in the router and map them back before `sanitize_filter_output` and `invalid_source_ids`, so stored drafts keep the real ids.
> Expect ~70% fewer filter output tokens and ~25% fewer history input tokens. Run the eval (groundedness).

**6. `commitments: skip extract.commitments for messages with no promise cue`**
> Every non-empty outgoing message triggers an LLM extraction, including "ок", "спасибо" and "+".
> Gate it with a recall-oriented variant of `drafting/promises.detect_promise`, plus date words and a length threshold.
> For agent sends of an unchanged approved draft, reuse the draft's `flags.creates_commitment` instead of extracting again.
> Run the us3 and jira commitment suites; add short-acceptance cases ("Ок, до пятницы").

**7. `drafting: reuse filter/deadline results on regenerate and cap regenerates`**
> 🔄 Regenerate reruns filter, earlier search, rank, deadlines and compose on identical inputs.
> Reuse the stored selection when `turn.last_message_id` is unchanged, and memoise `extract.deadlines` per (task, comment ids) for the day.
> Cap regenerates per draft (e.g. 3) and show a hint when the cap is reached.
> Cost and latency win; no prompt change.

**8. `ops: per-user LLM budget with graceful degradation`**
> The beta has no per-user spend limit. Using the spend from issue 1, add a configurable soft daily cap: past it, switch the user to the economy tier; past a hard cap, pause drafting and post a notice in the control chat; reset daily.
> Alert the operator on users above the 95th percentile.

---

## 8. Open items / not verified

- DeepSeek: whether reasoning tokens are billed exactly as output (very likely; not stated on the pricing page); the cache unit and interval sizes; whether caches are per account or per API key (it matters if several keys are used); the default `reasoning_effort` when none is sent.
- Thinking-token volumes per route are assumptions. Read them from `reasoning_tokens` in `llm_call` logs (already logged) after one day of hosted traffic.
- Traffic shape (turns per message, regenerate rate, share of contact profiles) should be replaced with real numbers from `drafts` and `activity` once issue 1 lands.
- The Anthropic prices come from the repo price table and the claude-api reference (cached 2026-10-06); they agree with each other.

Sources: [DeepSeek Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing), [DeepSeek Context Caching](https://api-docs.deepseek.com/guides/kv_cache), [DeepSeek Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode), [DeepSeek Change Log](https://api-docs.deepseek.com/updates), [DeepSeek API docs home](https://api-docs.deepseek.com/). Anthropic: the claude-api skill's `shared/prompt-caching.md` and model/pricing table (cached 2026-10-06).
