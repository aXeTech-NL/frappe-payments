import unittest
from unittest.mock import MagicMock, patch

import frappe

from payments import checkout
from payments.payment_gateways.doctype.mollie_settings import checkout as mollie_checkout
from payments.payment_gateways.doctype.mollie_settings.test_mollie_settings import settings


def remote_payment(**changes):
	payment = {
		"id": "tr_checkout1",
		"status": "open",
		"sequenceType": "oneoff",
		"mode": "test",
		"amount": {"value": "25.00", "currency": "EUR"},
		"metadata": {
			"payments_contract_version": 1,
			"payments_checkout_version": 1,
			"merchant_reference": "lms-order:one",
			"customer_reference": "opaque-customer",
		},
		"_links": {"checkout": {"href": "https://www.mollie.com/checkout/one"}},
	}
	payment.update(changes)
	return payment


class TestCheckout(unittest.TestCase):
	def setUp(self):
		self.controller = settings()
		self.controller._post = MagicMock(return_value=remote_payment())
		self.controller._get = MagicMock(return_value=remote_payment())
		self.patch = patch("payments.checkout.get_payment_gateway_controller", return_value=self.controller)
		self.patch.start()
		self.addCleanup(self.patch.stop)
		self.old_host = frappe.conf.get("host_name")
		frappe.conf.host_name = "https://checkout.example.test"
		self.addCleanup(lambda: frappe.conf.update(host_name=self.old_host))
		self.request = {
			"merchant_reference": "lms-order:one",
			"customer_reference": "opaque-customer",
			"amount": "25.00",
			"currency": "EUR",
			"description": "One course",
			"redirect_url": "https://checkout.example.test/lms/checkout/return/one",
			"webhook_url": checkout.configuration("Mollie-Primary")["webhook_url"],
			"idempotency_key": "order-one",
			"mode": "test",
			"account_fingerprint": checkout.configuration("Mollie-Primary")["account_fingerprint"],
		}

	def test_oneoff_does_not_create_customer_mandate_or_subscription(self):
		result = checkout.begin("Mollie-Primary", self.request)
		self.assertEqual(result["status"], "open")
		self.controller._post.assert_called_once()
		path, payload = self.controller._post.call_args.args
		self.assertEqual(path, "/payments")
		self.assertEqual(payload["sequenceType"], "oneoff")
		self.assertNotIn("customerId", payload)
		self.assertNotIn("mandateId", payload)
		self.assertNotIn("customer_email", payload["metadata"])

	def test_replay_keeps_provider_key_and_account_scopes_it(self):
		checkout.begin("Mollie-Primary", self.request)
		key = self.controller._post.call_args.kwargs["idempotency_key"]
		checkout.begin("Mollie-Primary", self.request)
		self.assertEqual(key, self.controller._post.call_args.kwargs["idempotency_key"])
		self.controller.gateway_name = "Other"
		self.request.update(self.controller.get_checkout_configuration())
		checkout.begin("Mollie-Other", self.request)
		self.assertNotEqual(key, self.controller._post.call_args.kwargs["idempotency_key"])

	def test_rejects_money_and_reference_changes(self):
		for change in (
			{"amount": {"value": "1.00", "currency": "EUR"}},
			{"mode": "live"},
			{"id": "invalid"},
			{"metadata": {**remote_payment()["metadata"], "customer_reference": "other"}},
		):
			with self.subTest(change=change):
				self.controller._post.return_value = remote_payment(**change)
				with self.assertRaises(ValueError):
					checkout.begin("Mollie-Primary", self.request)

	def test_rejects_recurring_resources_and_unsafe_checkout_link(self):
		for change in (
			{"sequenceType": "first"},
			{"customerId": "cst_unexpected"},
			{"_links": {"checkout": {"href": "https://evil.example.test/pay"}}},
		):
			with self.subTest(change=change), self.assertRaises(ValueError):
				self.controller._checkout_snapshot(remote_payment(**change))

	def test_rejects_unknown_fields_float_money_and_untrusted_urls_before_network(self):
		for changes in (
			{"amount": 25.0},
			{"extra": "field"},
			{"webhook_url": "https://other.test/hook"},
			{"redirect_url": "http://localhost/return"},
			{"mode": "live"},
			{"account_fingerprint": "another-account"},
		):
			with self.subTest(changes=changes), self.assertRaises(ValueError):
				checkout.begin("Mollie-Primary", {**self.request, **changes})
		self.controller._post.assert_not_called()

	def test_explicit_https_origin_required(self):
		for origin in (
			None,
			"http://localhost:8001",
			"https://user:pass@example.test",
			"https://example.test/path",
		):
			frappe.conf.host_name = origin
			with self.subTest(origin=origin), self.assertRaises(ValueError):
				checkout.configuration("Mollie-Primary")

	def test_fetch_requires_exact_id(self):
		with self.assertRaises(ValueError):
			checkout.fetch("Mollie-Primary", "tr_another")

	def test_partial_and_full_reversals_are_preserved(self):
		for value, expected in (("5.00", "partially_refunded"), ("25.00", "refunded")):
			self.controller._get.return_value = remote_payment(
				status="paid", amountRefunded={"value": value, "currency": "EUR"}
			)
			result = checkout.fetch("Mollie-Primary", "tr_checkout1")
			self.assertEqual(result["status"], expected)
			self.assertEqual(result["refunded_amount"], value)

	def test_delivery_uses_separate_generic_hook_and_propagates_failure(self):
		payment = checkout.begin("Mollie-Primary", self.request)
		with patch("payments.checkout.call_hook_method", side_effect=RuntimeError("retry me")) as hook:
			with self.assertRaises(RuntimeError):
				checkout.deliver("Mollie-Primary", payment)
			self.assertEqual(hook.call_args.args, ("checkout_payment_updated",))

	def test_webhook_ignores_posted_payment_state(self):
		frappe.form_dict = frappe._dict(id="tr_checkout1", status="paid", amount="0.01", member="victim")
		with (
			patch(
				"payments.payment_gateways.doctype.mollie_settings.webhook._resolve_controller",
				return_value=self.controller,
			),
			patch("payments.checkout.deliver") as deliver,
		):
			mollie_checkout.webhook("Mollie-Primary", "route-token")
			self.assertEqual(deliver.call_args.args[1]["status"], "open")
			self.assertEqual(deliver.call_args.args[1]["amount"], "25.00")

	def test_webhook_bad_token_never_fetches(self):
		frappe.form_dict = frappe._dict(id="tr_checkout1")
		with patch(
			"payments.payment_gateways.doctype.mollie_settings.webhook._resolve_controller",
			return_value=self.controller,
		):
			mollie_checkout.webhook("Mollie-Primary", "bad-token")
		self.controller._get.assert_not_called()

	def test_missing_capability_fails_closed(self):
		with patch("payments.checkout.get_payment_gateway_controller", return_value=object()):
			with self.assertRaises(checkout.CheckoutCapabilityError):
				checkout.configuration("Unsupported")
