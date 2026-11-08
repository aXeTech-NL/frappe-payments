# One-time checkout contract

`payments.checkout` is a server-only contract separate from `payments.recurring`.
It resolves the named Payment Gateway controller, validates bounded normalized
values, and never knows about LMS, Commerce orders or entitlements.

## Operations

- `configuration(gateway)` returns `mode` (`test`/`live`), HTTPS `webhook_url` and
  an opaque `account_fingerprint`. The latter changes when credentials/account
  routing changes, so consumers do not retry uncertain creations in another account.
  Keep this server-side. It is not an authentication credential.
- `begin(gateway, request)` requires exactly `merchant_reference`,
  `customer_reference`, positive fixed-point `amount`, ISO `currency`, `description`,
  HTTPS `redirect_url`, HTTPS `webhook_url`, `idempotency_key`, `mode`, and the
  previously obtained `account_fingerprint` (rechecked immediately before creation).
- `fetch(gateway, provider_payment_id)` retrieves an authoritative payment.
- `deliver(gateway, payment)` synchronously emits `checkout_payment_updated` with
  keyword arguments `payment_gateway` and `payment`. Exceptions propagate.

Payment snapshots use the strict money/reference/reversal validation shared with
`payments.recurring`, plus required `mode`. Mandate, subscription and provider
customer IDs are rejected: one-time checkout cannot silently create a recurring
agreement. Unknown fields, mismatched references, amount, currency, IDs and mode
fail closed. Checkout URLs must use HTTPS. Nothing here is whitelisted to clients.

## Mollie

The existing Frappe 16 Mollie Settings adapter also implements these capabilities.
It creates `/v2/payments` with `sequenceType=oneoff`, **without** creating a customer,
mandate or subscription. Metadata contains contract versions and opaque consumer
references only. Mutations use account/operation-scoped `Idempotency-Key` headers.
Only HTTPS Mollie checkout origins are returned.

The site must set an explicit HTTPS origin in `host_name`. Callback URLs never
come from the incoming request's Host header. API keys and routing tokens remain
encrypted Password fields on Mollie Settings; only System Managers manage them.
The callback token selects an account, it does not prove a payment.

Endpoint:

```
/api/method/payments.payment_gateways.doctype.mollie_settings.checkout.webhook
```

The provider posts only `id`. The route requires the configured gateway/token,
fetches the ID from that account, and ignores posted status, amount and ownership.
A recurring payment cannot be delivered through this one-time endpoint.

Unlike the existing recurring transport, one-time delivery has no queue handoff:
Frappe commits consumer writes before returning HTTP success. Remote/consumer
errors are not acknowledged as success, allowing Mollie to retry. Consumers must
make fulfillment atomic and idempotent, handle stale notifications, and schedule
reconciliation of stored provider IDs in case a webhook is lost. Do not commit
partially applied consumer writes or swallow consumer errors. This is not an
exactly-once delivery guarantee.

The owning app must durably save its order and exact request identity **before**
calling `begin`. It must reuse that identity after a timeout, not create a new
order. Never retry creation beyond the provider's idempotency retention window;
reconcile the old merchant reference instead. Gateway credential rotation also
requires reconciling uncertain creations, not recreating them in another account.

## Tests and activation

```
bench --site <isolated-site> run-tests --app payments --module payments.tests.test_checkout
bench --site <isolated-site> run-tests --app payments --module payments.tests.test_recurring
bench --site <isolated-site> run-tests --app payments --module payments.payment_gateways.doctype.mollie_settings.test_mollie_settings
bench --site <isolated-site> run-tests --app payments --module payments.payment_gateways.doctype.mollie_settings.test_webhook
```

Tests mock the remote Mollie API, not controller lookup or Frappe hook dispatch in
the LMS integration test. No real key is shipped or enabled. Before live use,
validate the actual Mollie test API, account-supported methods and a publicly
reachable HTTPS callback on a dedicated test site. Keep the recurring activation
path separate and require appropriate consent before using it.
