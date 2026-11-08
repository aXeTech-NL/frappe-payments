"""Server-only, provider-neutral one-time checkout contract.

Commerce owns orders and fulfilment. This module never creates a mandate,
subscription, enrollment or entitlement. Scalars share the recurring contract's
strict validation; one-time operations and hook delivery remain separate.
"""

from collections.abc import Mapping

from frappe.utils import call_hook_method

from payments import recurring as contract
from payments.utils import get_payment_gateway_controller


class CheckoutCapabilityError(NotImplementedError):
	pass


def _method(gateway, operation):
	method = getattr(get_payment_gateway_controller(gateway), operation, None)
	if not callable(method):
		raise CheckoutCapabilityError(f"Gateway does not support {operation}")
	return method


def configuration(gateway):
	data = _method(gateway, "get_checkout_configuration")()
	if not isinstance(data, Mapping) or set(data) != {"mode", "webhook_url", "account_fingerprint"}:
		raise ValueError("Invalid checkout configuration")
	return {
		"mode": contract._normalize_choice(data["mode"], "mode", {"test", "live"}),
		"webhook_url": contract._normalize_url(data["webhook_url"], "webhook_url"),
		"account_fingerprint": contract._normalize_reference(
			data["account_fingerprint"], "account_fingerprint"
		),
	}


def begin(gateway, request):
	required = {
		"merchant_reference",
		"customer_reference",
		"amount",
		"currency",
		"description",
		"redirect_url",
		"webhook_url",
		"idempotency_key",
		"mode",
		"account_fingerprint",
	}
	data = contract._validate_mapping_fields(request, "checkout", required, set())
	normalized = {
		field: contract._normalize_reference(data[field], field)
		for field in ("merchant_reference", "customer_reference", "idempotency_key", "account_fingerprint")
	}
	normalized.update(
		amount=contract._normalize_amount(data["amount"]),
		currency=contract._normalize_currency(data["currency"]),
		description=contract._normalize_text(data["description"], "description", max_length=255),
		redirect_url=contract._normalize_url(data["redirect_url"], "redirect_url"),
		webhook_url=contract._normalize_url(data["webhook_url"], "webhook_url"),
		mode=contract._normalize_choice(data["mode"], "mode", {"test", "live"}),
	)
	return normalize_payment(
		_method(gateway, "begin_checkout_payment")(contract._freeze(normalized)), expected=normalized
	)


def fetch(gateway, payment_id):
	payment_id = contract._normalize_reference(payment_id, "provider_payment_id")
	return normalize_payment(
		_method(gateway, "fetch_checkout_payment")(payment_id),
		expected={"provider_payment_id": payment_id},
	)


def normalize_payment(payment, *, expected=None):
	if not isinstance(payment, Mapping):
		raise ValueError("Invalid checkout payment")
	data = dict(payment)
	mode = contract._normalize_choice(data.pop("mode", None), "mode", {"test", "live"})
	if expected and "mode" in expected and mode != expected["mode"]:
		raise ValueError("Checkout payment mode changed")
	if any(
		data.get(field)
		for field in ("provider_customer_id", "provider_mandate_id", "provider_subscription_id")
	):
		raise ValueError("A one-time checkout cannot establish a recurring agreement")
	if data.get("checkout_url"):
		contract._normalize_url(data["checkout_url"], "checkout_url")
	return contract._freeze({**contract._normalize_payment_snapshot(data, expected=expected), "mode": mode})


def deliver(gateway, payment):
	"""Synchronous delivery in the caller's transaction; failures must propagate.

	The transport acknowledges only after consumers commit. Consumers must lock
	and deduplicate by account + payment ID, and handle out-of-order snapshots.
	"""
	call_hook_method("checkout_payment_updated", payment_gateway=gateway, payment=normalize_payment(payment))
