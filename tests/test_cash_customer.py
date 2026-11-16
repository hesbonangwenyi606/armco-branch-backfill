import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import inspect_cash_customer as inspect
from backfill_customer_branch import BackfillError


class CashCustomerTests(unittest.TestCase):
    def test_rule_probe_saves_rejection_without_changing_settings(self):
        from backfill_customer_branch import APIError
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(inspect, "settings", return_value={"ZOHO_ORG_ID": "1"}), \
             patch.object(inspect, "ReadOnlyClient") as client, patch("builtins.print") as printed:
            client.return_value.request.side_effect = [
                {"fields": [{"api_name": "cf_cash_customer"}]},
                APIError(404, {"code": 5, "message": "Unsupported route"})]
            inspect.main(["--output", folder, "--probe-validation-rules"])
            result = json.loads(printed.call_args.args[0])
            probe = result["validation_rules_probe"]
            self.assertEqual(probe["status"], "unavailable")
            self.assertEqual(probe["http_status"], 404)
            self.assertFalse(probe["documented_endpoint"])
            self.assertEqual(result["remote_updates_performed"], 0)
            self.assertTrue(list(Path(folder).rglob("rules_response.json")))
            self.assertTrue(all(call.args[0] == "GET" for call in client.return_value.request.call_args_list))

    def test_rule_probe_returns_only_matching_rule_identifiers(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(inspect, "settings", return_value={"ZOHO_ORG_ID": "1"}), \
             patch.object(inspect, "ReadOnlyClient") as client, patch("builtins.print") as printed:
            client.return_value.request.side_effect = [
                {"fields": [{"api_name": "cf_cash_customer"}]},
                {"code": 0, "validation_rules": [{"rule_id": "8", "name": "Cash Customer",
                    "condition": {"private": "PRIVATE CUSTOMER DATA"}}, {"rule_id": "9", "name": "Other rule"}]}]
            inspect.main(["--output", folder, "--probe-validation-rules"])
            result = json.loads(printed.call_args.args[0])
            self.assertEqual(result["validation_rules_probe"]["cash_customer_candidates"],
                             [{"rule_id": "8", "name": "Cash Customer"}])
            self.assertNotIn("PRIVATE CUSTOMER DATA", printed.call_args.args[0])

    def test_optional_field_does_not_imply_no_validation_rule(self):
        body = {"fields": [{"api_name": "cf_cash_customer", "field_id": "123",
                            "is_mandatory": False, "validation_message": "Private expression",
                            "default_value": "PRIVATE CUSTOMER VALUE"}]}
        result = inspect.summarize(body)
        self.assertFalse(result["field"]["is_mandatory"])
        self.assertEqual(result["validation_metadata_keys"], ["validation_message"])
        self.assertNotIn("PRIVATE CUSTOMER VALUE", json.dumps(result))
        self.assertNotIn("Private expression", json.dumps(result))
        self.assertIn("conditional", result["limitation"])

    def test_missing_ambiguous_or_malformed_metadata_fails(self):
        for body in ({}, {"fields": []}, {"fields": [None]},
                     {"fields": [{"api_name": "cf_cash_customer"}, {"label": "Cash Customer"}]}):
            with self.subTest(body=body), self.assertRaises(BackfillError):
                inspect.summarize(body)

    def test_command_only_reads_field_metadata_and_does_not_export_credentials(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(inspect, "settings", return_value={"ZOHO_ORG_ID": "1", "ZOHO_CLIENT_SECRET": "PRIVATE SECRET"}), \
             patch.object(inspect, "ReadOnlyClient") as client, patch("builtins.print") as printed:
            client.return_value.request.return_value = {"fields": [{"api_name": "cf_cash_customer", "is_mandatory": True}]}
            self.assertEqual(inspect.main(["--output", folder]), 0)
            client.return_value.request.assert_called_once_with("GET", "/settings/fields",
                query={"entity": "invoice", "filter_custom_fields": "true"})
            self.assertNotIn("PRIVATE SECRET", printed.call_args.args[0])
            for path in Path(folder).rglob("*.json"):
                self.assertNotIn("PRIVATE SECRET", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
