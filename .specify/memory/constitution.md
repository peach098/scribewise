# Scribe Agent Constitution

## Core Principles

### I. Human Approval Before Sending (NON-NEGOTIABLE)

Scribe drafts replies to the user's work messages; by default it MUST NOT send anything on
the user's behalf without the user explicitly approving that specific message.

- Every outbound message MUST pass through an approve / edit / reject step.
- Any auto-send mode MUST be opt-in, scoped (per channel, contact, or message type),
  reversible, and off by default.
- Every action the agent takes on an external system MUST be attributable and logged.

Rationale: a message sent in the user's name has real professional consequences and
cannot be unsent. The cost of one extra click is far lower than the cost of one wrong
message.

### II. Privacy & Data Minimization

Work messages contain confidential information about the user, their colleagues, and
their employer.

- The agent MUST collect, store, and send to third parties (including LLM providers)
  only the data needed for the current task.
- Message content and style profiles MUST NOT be committed to the repository, written
  to logs in plaintext, or used in test fixtures; fixtures MUST be synthetic or
  anonymized.
- Credentials and tokens MUST come from environment variables or a secret store, never
  from source code.
- The user MUST be able to view and delete everything the agent stores about them.

Rationale: a leak would harm people who never agreed to use the tool.

### III. Grounded Replies, No Fabrication

Drafts MUST be grounded in the conversation, the user's provided context, and their
previous messages.

- The agent MUST NOT invent facts, commitments, dates, numbers, or decisions.
- When information needed for a reply is missing, the agent MUST either ask the user or
  leave a clearly marked placeholder in the draft.
- Drafts that commit the user to something (deadlines, agreements, money) MUST be flagged
  for extra attention in the approval step.

Rationale: a reply that sounds like the user but says something untrue is worse than no
reply.

### IV. Faithful Personal Style

Drafts MUST read as if the user wrote them: tone, length, formality, language, and
typical phrasing, adjusted for the recipient and channel.

- The style model MUST be learned from the user's own messages, and the user MUST be able
  to inspect and correct it.
- Style MUST NOT override correctness (Principle III) or safety (Principle I).

Rationale: matching the user's style is the product's core value, but it is never a
reason to bend the facts.

### V. Evaluation-Driven Development

Changes to prompts, models, retrieval, or style logic MUST be measured, not judged by
feel.

- The project MUST keep a versioned evaluation set of synthetic or anonymized message
  threads with expected-reply criteria (groundedness, style match, safety).
- Any change affecting draft generation MUST run the evaluation set and MUST NOT ship if
  it regresses an agreed metric without documented justification.
- Deterministic code (parsing, integrations, storage) MUST have automated tests, written
  before or together with the code.

Rationale: LLM behavior changes silently; only repeatable evaluation catches
regressions.

### VI. Simplicity & Observability

- Start with the simplest design that works (YAGNI). Each new dependency, service, or
  abstraction MUST be justified in the plan.
- LLM providers and messaging integrations MUST sit behind narrow interfaces so they can
  be swapped or mocked in tests.
- The agent MUST emit structured logs and traces (prompt version, model, latency, cost,
  decision taken) with message content redacted per Principle II.

Rationale: a small, observable system is easier to debug, secure, and change.

## Security & Data Handling

- Integrations with messaging platforms MUST request the minimum OAuth scopes needed.
- Stored message data and style profiles MUST be encrypted at rest and have a defined
  retention period.
- Content from incoming messages is untrusted input: it MUST NOT be able to change the
  agent's instructions, trigger tool actions, or bypass the approval step
  (prompt-injection resistance).
- Third-party LLM providers MUST be configured so that user data is not used for model
  training where the provider allows it.

## Development Workflow & Quality Gates

- Work follows the Spec Kit flow: specify → (clarify) → plan → tasks → implement.
- Every plan MUST include a Constitution Check confirming compliance with Principles I–VI,
  or documenting a justified exception.
- Every change goes through a pull request with passing automated tests and, where draft
  generation is affected, an evaluation run (Principle V).
- Secrets scanning and dependency checks MUST pass before merge.

## Governance

- This constitution overrides other project practices. Where they conflict, the
  constitution wins.
- Amendments are made via a pull request that edits this file, explains the reason, and
  describes any migration needed for existing specs, plans, or code.
- Versioning follows semantic versioning: MAJOR for removing or redefining a principle,
  MINOR for adding a principle or section or materially expanding guidance, PATCH for
  clarifications and wording.
- Compliance is checked during plan review (Constitution Check) and code review. Any
  exception MUST be recorded in the relevant plan with its justification.

**Version**: 1.0.0 | **Ratified**: 2026-10-16 | **Last Amended**: 2026-10-16
