# Expanded local contracts

This increment covers merchant and platform billing initiation, automatic
local webhooks, and additional Stripe resource simulations. These
remain synthetic API contracts, not claims of real provider equivalence.

## Billing

Portal configurations are account scoped and durable. The supported feature
projection includes a dedicated period-end cancellation policy. Checkout
supports one existing monthly or annual USD price and an
optional connected-account application-fee percentage. Completion creates
linked synthetic resources atomically. Synthetic `LOCAL_` promotion fixtures
support one percentage discount with `once` or `forever` duration; pass their
ID as `discounts[0][promotion_code]`. Checkout, subscription and invoice reads
can expand their associated `discounts`. Once a period is due, a versioned
subscription control creates a renewal invoice with either a paid outcome or
an open failed outcome. Failed invoices require an explicit retry before the
subscription may recover or cancel. Monthly and annual periods retain their
original calendar-day anchor. No automatic billing clock, proration, trial,
multiple-item subscription, or real provider billing is modeled.

A run-scoped admin control configures a
synthetic account tax rate in basis points; without it, automatic tax fails
closed. The configured rate is captured when Checkout is created and applied
to its one price as a fixed exclusive rate. This is a test calculation, not a
jurisdiction, registration, tax determination or collected tax identity.
Address and tax-ID request flags are recorded but no actual details are collected
or verified. Unsupported Checkout options fail closed. Production database
and live-provider qualification are separate.

## Saved cards and off-session payments

Use the loopback listener, the private bootstrap API key, `Stripe-Version`,
`X-Local-Simulator-Instance`, `X-Local-Simulator-Run`, and `Stripe-Account`
for the connected account. Use form-encoded bodies and an `Idempotency-Key`
on each `/v1` POST. Money amounts are integer USD cents.

1. `POST /v1/customers` with `email=buyer@example.test` creates a synthetic
   customer. `POST /v1/payment_intents` with `amount`, `currency=usd`,
   `customer=<cus>`, and `setup_future_usage=off_session` starts an on-session
   payment. `capture_method=automatic` is the Endive default. Set
   `capture_method=manual` to capture separately. In Endive, omit the card
   selection or send `allowed_payment_method_types[]=card`. In Acacia, send
   `payment_method_types[]=card` and an explicit `capture_method`. Both intent
   creation forms also accept `application_fee_amount` and `metadata[...]`.
2. `GET /v1/payment_intents/{pi}` returns the initial intent's temporary
   `client_secret`. Send JSON `{"client_secret":"<secret>","account":"<acct>","purpose":"card_payment"}`
   to `POST /__simulator/v1/components/exchange` with the bootstrap API key and
   the same instance, run, and version headers. Send the returned
   `control_capability` and `provider_version` as
   `{"control_capability":"<cap>","account":"<acct>","purpose":"card_payment","action":"confirm","expected_provider_version":1}`
   to `POST /__simulator/v1/components/actions`, substituting the current
   integer version. If the scenario requires authentication, use
   `action=complete_authentication` with the new version from the confirmation.
3. Automatic capture happens during the successful component action. For
   manual capture, `POST /v1/payment_intents/{pi}/capture` with
   `amount_to_capture=<full amount>`. The succeeded intent and `latest_charge`
   carry a new `payment_method` ID. `GET /v1/payment_methods/{pm}` with the
   bootstrap API key returns `id`, `object=payment_method`, `type=card`,
   `customer`, `initial_payment_intent`, `status=active`, `created`, and
   `livemode=false`. The instrument is created only during capture.
4. For a renewal, `POST /v1/payment_intents` with `amount`, `currency=usd`,
   and the same `customer`. Use automatic capture, with the card selection
   for the instance's profile. This creates an intent with `off_session=true`.
   `POST /v1/payment_intents/{pi}/confirm` with `off_session=true` and
   `payment_method=<pm>` accepts only a saved instrument in that run, account,
   and customer. Direct `pm_card_visa` confirmation returns HTTP 400 for both
   setup and off-session intents. Off-session intent reads return
   `client_secret=null`.

Successful off-session confirmation returns HTTP 200 with `status=succeeded`
and the captured charge, and queues the usual `payment_intent.succeeded`
event. The existing admin `POST /__simulator/v1/runs/{run}/actions` accepts
`decline_next_confirmation` or `require_authentication_next` in `action`
with the current integer `expected_state_version`. These produce HTTP 402
with `error.type=card_error`, `error.payment_intent`, and an `error.code` of
`card_declined` or `authentication_required`. The intent becomes
`requires_payment_method` or `requires_action`, respectively, with the same
code in `last_payment_error` and no charge. An interaction-required attempt
can be canceled with `POST /v1/payment_intents/{pi}/cancel` and an empty form.
Any replacement payment starts on-session. `process_next_confirmation` applies
only to on-session card confirmation.

To change availability, use the admin bearer and JSON on
`POST /__simulator/v1/runs/{run}/resources/inspect` with
`{"kind":"payment_method","resource_id":"<pm>","account":"<acct>"}` to get
`provider_version`. Then call
`POST /__simulator/v1/runs/{run}/payment_methods/{pm}/actions` with
`{"account":"<acct>","action":"<action>","expected_provider_version":1}`,
using the observed version. `make_unavailable`, `restore`, and `revoke`
return `status` and the new `provider_version`. Revocation is permanent.
Confirmation returns HTTP 409 `simulator_payment_method_unavailable` or
`simulator_payment_method_revoked` for those states, and HTTP 403
`simulator_resource` for unknown or foreign instruments.

After a lost reply, read the intent with `GET /v1/payment_intents/{pi}` or
replay the original `/v1` POST with the same key, headers, and form bytes.
Replays return the stored response, including HTTP 402 outcomes, after
restart. Reusing a key with changed input returns HTTP 409. The adapter owns
enrollment consent, renewal scheduling, and any decision to initiate a new
attempt. All instruments and outcomes are synthetic.

## Automatic webhooks

Manual delivery stays the default. When enabled with a pinned literal-loopback
callback, the worker starts idle. After the consumer installs the current boot's
webhook secret, `POST /__simulator/v1/runs/{run}/webhooks/start` with an empty
JSON body activates the worker for all active runs. Activation is required again
after restart because credentials rotate. The worker claims each newly queued
event once by default. Optional `--webhook-max-attempts` (1–10) and
`--webhook-retry-delay` (1–3600 seconds) enable exponential backoff, capped at
one hour. The due time and attempt count survive restart. The worker retries
failed deliveries but never automatically replays an interrupted in-flight
attempt; those require inspection and manual action. A manual retry of a
scheduled event retains its automatic policy. The worker stops and joins before
the store closes. Reset and manual delivery share its reservation lock,
preserving run isolation.

## Payout model

The expanded synthetic balance contract credits a completed charge to the
charge account's pending balance, net of the configured synthetic processing
fee and any application fee. The application fee is credited to the platform's
pending balance. A version-checked admin action settles transactions whose
configured delay has elapsed (default 48 hours); pending funds cannot be paid
out. Card and bank-account sources remain separate for payout purposes.
Successful
refunds debit the merchant's available balance immediately, even if that makes
it negative; pending, failed and canceled refunds do not debit it. A fee refund
debits the platform and credits the merchant. A dispute reserves the disputed
amount from available funds and a win releases it. Those debits are synthetic
test rules and do not assert real settlement, tax or fee calculation.

Each new balance transaction is scoped to its run and provider account. Older
charges already stored before this feature are not retroactively credited.
Idempotent replays do not create another transaction. All resource, outbox and
balance changes for a mutation must commit or roll back together.

An administrator funds a synthetic account balance using a version-checked
control. Available funds are separate per provider account and run. Creating a
pending USD payout reserves funds atomically; a version-checked failure or a
cancellation returns the reservation, while a paid outcome consumes it.
Payout events and state survive restart; repeated idempotency keys replay the
original response, and stale lifecycle controls fail. No actual bank details,
real money movement, real settlement schedules, provider processing costs, or
application ledger are modeled. Processing fees are configurable synthetic
fixtures, with zero processing fees by default.

## Bank payments and Accounts v2

Endive accepts explicit `us_bank_account` PaymentIntents with automatic capture
and the synthetic `pm_usBankAccount_processing` and
`pm_usBankAccount_unverified` identifiers. The latter exercises a synthetic
three-attempt descriptor-code verification flow using `SM11AA`. Synthetic
mandates transition from pending to active and become inactive after an
unauthorized debit or dispute. Account-scoped controls resolve payment and
dispute outcomes. No real bank details, customer consent, verification, or
settlement are collected or established. Acacia remains card-only.

Accounts v2 accepts a JSON request for the synthetic US merchant/card capability
subset, using only `@example.test` contact addresses and synthetic display
names. `include` exposes the requested identity, merchant configuration and
requirements projections. A version-checked admin control toggles synthetic
identity requirements and capability status. Account creation and requirement
changes produce thin v2 events with a retrievable account reference. No real
identity fields or verification are accepted, and these accounts do not
automatically become v1 connected accounts. Other Stripe products and fields
fail closed.

Stripe references for the modeled shapes: [portal configuration creation](https://docs.stripe.com/api/customer_portal/configurations/create),
[payout creation](https://docs.stripe.com/api/payouts/create),
[ACH payment lifecycle](https://docs.stripe.com/payments/ach-direct-debit/accept-a-payment?payment-ui=direct-api),
and [Accounts v2](https://docs.stripe.com/api/v2/core/accounts/create).
Local behavior is intentionally narrower than those provider APIs.
