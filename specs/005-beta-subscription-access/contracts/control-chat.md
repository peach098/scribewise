# Contract: Control-Chat Behaviour Under the Access Gate

Everything here applies only with `SCRIBE_ACCESS_GATE=true`. With the gate off, the bot
behaves as it does today.

The gate sits after the existing private-chat filter. Group chats stay ignored, and no
"no access" text is ever posted there.

## Decision per update

| Sender | Users row | Result |
|---|---|---|
| In `SCRIBE_OPERATOR_TELEGRAM_IDS` | any | **allow** |
| Has access (beta and/or active subscription) | any | **allow**. Normal handlers. `/start` without a row creates one as today |
| No access | none | **refuse** |
| No access | exists | **restricted** |

### Refuse

The handler does not run, so no user row, no FSM state and no LLM call is created. The
`refused_contacts` row is upserted. If the cooldown allows, the bot sends `access.refused`;
otherwise it sends nothing. A callback query is always answered with an empty
`answerCallbackQuery` so the client's spinner stops. If the cooldown allows, the
`access.refused` text is also sent to the chat as a message, so the person sees why the
button did nothing. An inline query gets an empty result.

### Restricted

These pass to their normal handlers:

- `/my_data` and callbacks whose data starts with `mydata:`;
- `/delete_my_data` and the text message in `DeleteStates.confirm`;
- `/cancel` while in `DeleteStates.confirm`.

Everything else gets `access.ended_reply`, under the same cooldown as refuse. Other
callbacks are answered silently.

## Texts (i18n keys, en and ru)

Each text uses the sender's language: the stored `users.language`, otherwise their
Telegram `language_code`.

| Key | When | Content (en) |
|---|---|---|
| `access.refused` | Refuse | "Scribe is in closed beta right now. You can apply to join the beta, or learn about the subscription." Buttons: **Join the beta** (`apply:start`), **Subscription** (`apply:subinfo`) |
| `access.ended_reply` | Restricted, any blocked message | "Your access to Scribe has ended, so I'm not reading your chats or preparing replies. Your data is kept. You can still view it with /my_data or delete it with /delete_my_data." Same two buttons |
| `access.apply_pending` | Refuse or restricted while an application is pending (replaces the two texts above, no buttons) | "Your application to join the beta is being reviewed. I'll write to you as soon as there is a decision." |
| `access.apply_rejected_recently` | Refuse or restricted within 30 days after a rejection (no buttons) | "The beta is full for now. You can apply again after {date}." |
| `access.ended_notice` | Once, from `access_reconcile` on **lost** | "Your access to Scribe has ended ({reason_text}). I've stopped reading your chats. Replies waiting for your approval were not sent. Your data is kept: /my_data, /delete_my_data. To get access back, send /start." `reason_text` is `subscription ended` or `beta access removed` |
| `access.restored_notice` | Once, from `access_reconcile` on **regained** | "Your access to Scribe is back. I'm working again with your previous settings." Plus `access.reconnect_hint` ("Your Telegram connection has expired; reconnect with /telegram") if the session is not active |
| `access.delete_warning` | Added to the existing `/delete_my_data` confirmation when the person has a beta entry or an active or scheduled subscription | "Deleting your data also removes {what}. To use Scribe again, you'll need to ask for access again." `{what}` is one of `access.delete_what_beta`, `access.delete_what_subscription` (with the end date) or `access.delete_what_both` |
| `access.sub_reminder` | Once, 3 days or less before `ends_at` | "Your Scribe subscription ends on {date}. To renew: {renew}" (`subscription_contact` if set, otherwise \"send /start\") |

- There is no external request link (removed 2026-10-22). `SCRIBE_SUBSCRIPTION_CONTACT` is the only configurable contact.
- The texts never mention other users or the reason a beta entry was removed (the
  operator's note is not shown).
- `/my_data` output gains an **Access** section: status, beta since, subscriptions (start,
  end, state), and the count of access events. The JSON export gains `access.beta`,
  `access.subscriptions` and `access.events`.

## Command menu

There is no change for allowed users. Refused senders get no menu change; Telegram shows
the default bot menu, and each command gets the refusal under the cooldown.

## Beta application flow (US5)

| Step | Trigger | Bot |
|---|---|---|
| Start | `apply:start` (any sender without access, no pending or recent rejection) | `access.apply_ask`: "How do you plan to use Scribe? A sentence or two is enough: which chats, what kind of replies." Sets `ApplyStates.use_case`. Shows a **Cancel** button (`apply:cancel`) |
| Use case | Text in `ApplyStates.use_case` | 10–1000 chars: `access.apply_review` with the text quoted, then the note `access.apply_consent` ("By pressing «Send application» you agree to the processing of your personal data."). Buttons: **Send application** (`apply:send`), **Edit** (`apply:edit`), **Cancel** (`apply:cancel`). Sets `ApplyStates.review`. Otherwise `access.apply_too_short`, `access.apply_too_long` or `access.apply_text_only`, and the state stays |
| Edit | `apply:edit` | Back to the start step |
| Cancel | `apply:cancel` or `/cancel` in `ApplyStates` | `access.apply_cancelled`, FSM cleared, nothing stored |
| Send | `apply:send` in `ApplyStates.review` | Stores the application. `access.apply_sent`: "Thanks! Your application has been sent. I'll write to you when there is a decision." Each operator gets `access.apply_operator` |
| Subscription | `apply:subinfo` | `access.subscription_placeholder`: "Subscriptions are coming soon. Prices aren't set yet. Meanwhile you can apply to join the beta." Plus `access.subscription_line` if `SCRIBE_SUBSCRIPTION_CONTACT` is set. Stores nothing |

**Operator message** (`access.apply_operator`, operator's language), plain text:

```
New beta application
@username (or "no username") · id 900000001 · ru
Use case:
<use case text>
```

Buttons: **Approve** (`betaapp:approve:<uuid>`), **Reject** (`betaapp:reject:<uuid>`).

After a decision, every copy is edited to append "✅ Approved" or "❌ Rejected", the
operator id (or `CLI`) and the time, and the buttons are removed.

**Applicant notices**:
- `access.apply_approved`: "You're in the Scribe beta! Send /start to set up."
- `access.apply_rejected`: "Thank you for your interest! The beta is full for now. You can
  apply again after {date}."
