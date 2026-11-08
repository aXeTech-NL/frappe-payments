"""Mollie single-payment adapter. No customer, mandate or recurring sequence."""

import hashlib
from urllib.parse import urlencode, urlsplit

import frappe

from payments import checkout


class MollieCheckoutMixin:
	def get_checkout_configuration(self):
		self._require_enabled()
		# Do not derive callbacks from the incoming Host header.
		origin = (frappe.conf.get("host_name") or "").rstrip("/")
		parsed = urlsplit(origin)
		if (
			parsed.scheme != "https"
			or not parsed.netloc
			or parsed.path
			or parsed.query
			or parsed.fragment
			or parsed.username
		):
			raise ValueError("Mollie checkout requires an explicit HTTPS host_name origin")
		token = self.get_password("webhook_token", raise_exception=False)
		if not token:
			raise ValueError("Mollie webhook routing token is missing")
		return {
			"mode": self._checkout_mode(),
			"account_fingerprint": hashlib.sha256(
				("mollie:" + self.payment_gateway + ":" + self.get_password("api_key")).encode()
			).hexdigest(),
			"webhook_url": origin
			+ "/api/method/payments.payment_gateways.doctype.mollie_settings.checkout.webhook?"
			+ urlencode({"gateway": self.payment_gateway, "token": token}),
		}

	def _checkout_mode(self):
		key = self.get_password("api_key", raise_exception=False) or ""
		if not key.startswith(("test_", "live_")):
			raise ValueError("Invalid Mollie API key")
		return "test" if key.startswith("test_") else "live"

	def begin_checkout_payment(self, request):
		from .mollie_settings import _derived_idempotency_key

		config = self.get_checkout_configuration()
		if any(request[field] != config[field] for field in ("webhook_url", "mode", "account_fingerprint")):
			raise ValueError("Mollie checkout configuration changed")
		payment = self._post(
			"/payments",
			{
				"amount": {"currency": request["currency"], "value": request["amount"]},
				"description": request["description"],
				"sequenceType": "oneoff",
				"redirectUrl": request["redirect_url"],
				"webhookUrl": request["webhook_url"],
				"metadata": {
					"payments_contract_version": 1,
					"payments_checkout_version": 1,
					"merchant_reference": request["merchant_reference"],
					"customer_reference": request["customer_reference"],
				},
			},
			idempotency_key=_derived_idempotency_key(
				self.gateway_name, request["idempotency_key"], "checkout"
			),
		)
		return self._checkout_snapshot(payment)

	def fetch_checkout_payment(self, payment_id):
		from .mollie_settings import _require_mollie_id

		self._require_enabled()
		_require_mollie_id(payment_id, "tr_", "provider_payment_id")
		return checkout.normalize_payment(
			self._checkout_snapshot(self._get(f"/payments/{payment_id}")),
			expected={"provider_payment_id": payment_id},
		)

	def _checkout_snapshot(self, payment):
		from .mollie_settings import _payment_snapshot

		if (
			payment.get("sequenceType") != "oneoff"
			or (payment.get("metadata") or {}).get("payments_checkout_version") != 1
		):
			raise ValueError("Not a one-time checkout payment")
		if payment.get("mode") != self._checkout_mode():
			raise ValueError("Mollie payment mode mismatch")
		snapshot = checkout.normalize_payment({**_payment_snapshot(payment), "mode": payment["mode"]})
		if snapshot.get("checkout_url"):
			url = urlsplit(snapshot["checkout_url"])
			if url.scheme != "https" or not (
				url.hostname == "mollie.com" or (url.hostname or "").endswith(".mollie.com")
			):
				raise ValueError("Unexpected Mollie checkout origin")
		return snapshot


@frappe.whitelist(allow_guest=True, methods=["POST"])  # nosemgrep
def webhook(gateway: str = "", token: str = ""):
	"""The POSTed id is only a hint. Never accept status or money from a browser.

	No early commit/queue handoff: Frappe commits the consumer transaction before
	acknowledging the webhook. Provider/API/consumer failures remain retryable.
	Commerce additionally reconciles stored payment IDs to recover missed hints.
	"""
	from .mollie_settings import MollieResourceNotFound
	from .webhook import _PAYMENT_ID_RE, MollieWebhookRejected, _resolve_controller

	payment_id = frappe.form_dict.get("id")
	if not isinstance(payment_id, str) or not _PAYMENT_ID_RE.fullmatch(payment_id):
		return
	try:
		controller = _resolve_controller(gateway)
		if not controller.webhook_token_matches(token):
			return
	except MollieWebhookRejected:
		return
	try:
		payment = checkout.fetch(gateway, payment_id)
	except MollieResourceNotFound:
		return
	checkout.deliver(gateway, payment)
