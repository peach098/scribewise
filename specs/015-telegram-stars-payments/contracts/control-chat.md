# Contract: Control Chat (payments)

Extends `specs/005-beta-subscription-access/contracts/control-chat.md`. All texts exist in
`en` and `ru` (i18n parity test). `{price}` is a number followed by ` ⭐`. Dates use
`format_day_month_year` in the person's timezone (UTC for people without an account).

"Payments available" means `access_gate` and `payments_enabled` are on and `stars_price` is
set (research P9).

## Gate allow-list (research P7)

Passes the gate for anyone, without counting against the refusal cooldown:

| Update | Handler |
|---|---|
| Message with `successful_payment` | `payments.bot.on_successful_payment` |
| Message with `refunded_payment` | `payments.bot.on_refunded_payment` |
| Callback `pay:*` | `payments.bot` |
| `/paysupport`, `/terms`, `/cancel` and any message in `PaySupportStates` | `payments.support` |

`pre_checkout_query` and `subscription` updates are separate observers with no gate. Group
chats are ignored, as today. `pre_checkout_query` has no chat and is accepted from anyone.

## Callbacks

| Data | Who | Effect |
|---|---|---|
| `apply:subinfo` (existing) | anyone | Offer (if payments available and no running Stars subscription), manage view (if running), else the placeholder of specs/005 |
| `pay:offer` | anyone | Same as above; used in reminders and `/account` |
| `pay:cancel` | payer of a running subscription | Ask to confirm: `pay.cancel_confirm` with **Yes, cancel** (`pay:cancel:yes`) / **Keep** (`pay:cancel:no`) |
| `pay:cancel:yes` | same | `editUserStarSubscription(is_canceled=True)`; → `pay.canceled` |
| `pay:resume` | payer of a `canceled` subscription before `period_end` | `editUserStarSubscription(is_canceled=False)`; → `pay.resumed` |

On a Telegram error the callback answers `pay.try_again` and changes nothing. Any callback
whose condition no longer holds answers `pay.stale` and refreshes the view.

## Offer (US1)

```text
pay.offer:
  Scribe subscription: {price} every 30 days.
  Includes: drafts of replies in your work chats in your style, ... (fixed text)
  Renews automatically until you cancel; cancel any time in /account.
  By subscribing you accept the /terms.
[ Subscribe for {price} ]   ← URL button: invoice link (research P1)
```

- If the person has beta access or an operator subscription, one line is added before the
  button. It is `pay.offer_has_beta` ("You already have beta access; a subscription keeps you
  in after the beta.") or `pay.offer_has_sub` ("You already have access until {date}.").
- The link is made per offer. Pressing an old link after 1 hour is declined at pre-checkout.

## Pre-checkout decline reasons (`error_message`, research P3)

| Reason | Key |
|---|---|
| payments turned off | `pay.declined_off` |
| price changed / amount differs | `pay.declined_price` |
| offer older than 1 h or unknown | `pay.declined_expired` ("This offer has expired. Press Subscription again.") |
| already has a running Stars subscription | `pay.declined_running` |
| internal error or timeout | `pay.declined_error` |

## After payment

| Event | Message to the person |
|---|---|
| First payment credited | `pay.thanks`: "Subscription active. Next charge {date}: {price}. Cancel any time in /account." Plus `pay.thanks_start` ("Send /start to set up.") when they have no account, or `access.restored_notice` behaviour from specs/005 when they were suspended |
| Renewal credited | nothing (FR-006) |
| Backfilled payment (research P4) | the same as the first-payment message, if it was a first payment; nothing for a renewal |
| Refund | `pay.refunded`: "Your payment of {price} from {date} was refunded. The subscription is cancelled." |
| `subscription` update `failed` | nothing at once; when the period ends: `pay.ended_failed` ("The renewal charge failed, so your subscription ended.") with **Subscription** |
| Period ended after cancel | `pay.ended_canceled` with **Subscription** |
| No renewal by `period_end + 1 h` | `pay.ended_expired` ("Your subscription was not renewed, so it ended.") with **Subscription** |
| Access lost through a refund | nothing more: `pay.refunded` already explains it |

The specs/005 `access.ended_notice` is replaced by the `pay.ended_*` text when the last
access source was a Stars subscription.

## `/account` (new command, US2 scenario 3)

Added to `COMMAND_NAMES` after `settings`. It answers in any idle state and also for
restricted users. The `access_lines` of `/my_data` are reused:

```text
pay.account:
  Access: {beta | subscription | beta and subscription | operator | none}
  [Stars subscription: renews on {date} for {price}]   [ Cancel subscription ]
  [Stars subscription: cancelled, active until {date}] [ Turn renewal back on ]
  [Subscription until {date} (granted by the operator)]
  [ Subscription ]   ← only when payments available and nothing running
  Payment help: /paysupport · Terms: /terms
```

## Reminders (research P6)

| Case | Key | Button |
|---|---|---|
| Stars `active`, 3 days before `period_end` | `pay.renew_reminder`: "Your subscription renews on {date} for {price}." | **Cancel subscription** (`pay:cancel`) |
| Stars `canceled`, 3 days before | `pay.end_reminder`: "Your subscription ends on {date}." | **Turn renewal back on** (`pay:resume`) |
| Operator subscription, payments available | existing `access.sub_reminder` with `renew` = `pay.renew_with_stars` | **Subscription** (`pay:offer`) |

## `/paysupport` (US3, research P11)

1. The bot sends `pay.support_intro`, then waits for `PaySupportStates.describe`. The intro
   says how payments work, how to cancel and how refunds are handled, and asks the person to
   describe the problem in one message.
2. Text of 1–1000 characters goes to every operator as plain text:
   `pay.support_operator`, which shows `{who} · id {telegram_id}`, the payments
   (`{charge_id} {date} {amount}` for the last 90 days, or "none") and the text. The person
   gets `pay.support_sent`.
3. Other input gets the request again. `/cancel` leaves the flow. A second request within
   10 minutes gets `pay.support_wait`.
4. With no operator configured, the person gets `pay.support_contact` with
   `subscription_contact`, or a generic line if that is empty. Nothing is stored.

## `/terms`

`pay.terms` gives:

- the service in one sentence;
- the price per 30 days, or "prices not set" when payments are not available;
- automatic renewal and how to cancel;
- "refunds: on request through /paysupport, decided by the operator";
- "after you delete your data, a minimal payment record (Telegram id, amount, date, payment
  reference) is kept for 3 years for accounting";
- the contact.

## Data commands (FR-019, FR-020)

- `/my_data`: the access section gains the payments (`{date} {amount} {first|renewal}
  [refunded {date}]`) and the Stars subscription state.
- `/delete_my_data` warning: with a running Stars subscription, add
  `pay.delete_warning_running` ("Your Stars subscription will be cancelled. Paid days left are
  lost and not refunded automatically."). Always add, if any payment exists,
  `pay.delete_warning_record` ("A minimal record of your payments is kept for 3 years.").
- If cancelling on Telegram fails at confirmation, the bot replies `pay.delete_cancel_failed`,
  nothing is deleted, and the person can retry.

## Command menu

`/account`, `/paysupport` and `/terms` are added to `COMMAND_NAMES`, with descriptions in both
languages. `/paysupport` and `/terms` are listed even when payments are off: Telegram asks
bots that take payments to answer them, and they are harmless otherwise.
