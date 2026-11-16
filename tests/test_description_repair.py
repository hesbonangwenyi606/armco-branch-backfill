import unittest
from copy import deepcopy

from backfill_customer_branch import BackfillError
from repair_test_invoice import check_plan, projection
from test_backfill import document, invoice_schema


class DescriptionRepairTests(unittest.TestCase):
    def setUp(self):
        self.row = {"type": "invoice", "zoho_id": "1462401000001974003", "number": "152522",
                    "branch_record_id": "009"}
        self.schema = invoice_schema()
        self.original = document(self.row)
        self.original["adjustment"] = 0
        self.original["adjustment_description"] = ""
        self.current = document(self.row, "009")
        self.current.update(adjustment=0, adjustment_description="Adjustment")

    def test_repair_payload_changes_only_original_description(self):
        self.assertEqual(check_plan(self.original, self.current, self.row, self.schema),
                         {"adjustment_description": ""})

    def test_unrelated_changes_wrong_invoice_or_branch_block_repair(self):
        for field, value in (("total", 999), ("adjustment", 1), ("customer_id", "other")):
            current = deepcopy(self.current)
            current[field] = value
            with self.subTest(field=field), self.assertRaises(BackfillError):
                check_plan(self.original, current, self.row, self.schema)
        with self.assertRaises(BackfillError):
            check_plan(self.original, self.current, {**self.row, "zoho_id": "other"}, self.schema)

    def test_already_restored_does_not_create_write(self):
        self.current["adjustment_description"] = ""
        self.assertIsNone(check_plan(self.original, self.current, self.row, self.schema))

    def test_payment_link_normalization_retains_other_link_changes(self):
        before, after = deepcopy(self.original), deepcopy(self.original)
        before["invoice_url"] = "https://zohosecurepay.com/pay?CInvoiceID=old&other=1"
        after["invoice_url"] = "https://zohosecurepay.com/pay?CInvoiceID=new&other=1"
        self.assertEqual(projection(before, "7", self.schema), projection(after, "7", self.schema))
        after["invoice_url"] += "&unexpected=1"
        self.assertNotEqual(projection(before, "7", self.schema), projection(after, "7", self.schema))


if __name__ == "__main__":
    unittest.main()
