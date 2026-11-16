import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock

from backfill_customer_branch import BackfillError
from find_creditnote_candidate import classify, ReadOnlyClient, scan
from test_backfill import row, document, invoice_schema


class CandidateTests(unittest.TestCase):
    def test_linked_invoice_required_not_just_credited_invoices(self):
        doc = document(row("creditnote"))
        doc["invoices_credited"] = [{"invoice_id": "55"}]
        self.assertEqual(classify(doc, "7", invoice_schema()), "missing_invoice_association")
        doc.update(invoice_id="55", invoice_number="INV55")
        self.assertEqual(classify(doc, "7", invoice_schema()), "candidate_requires_guarded_test")
        doc["custom_fields"][0]["value"] = "9"
        self.assertEqual(classify(doc, "7", invoice_schema()), "branch_present")

    def test_readonly_client_rejects_writes(self):
        client = ReadOnlyClient({})
        with self.assertRaises(BackfillError):
            client.request("PUT", "/creditnotes/1", {})

    def test_scan_bounded_and_stops_at_first_candidate(self):
        rows = [row("creditnote", str(n)) for n in range(5)]
        docs = [document(r) for r in rows]
        docs[1].update(invoice_id="55", invoice_number="INV55")
        client = Mock()
        client.read.side_effect = docs
        with tempfile.TemporaryDirectory() as temp:
            result = scan(client, rows, invoice_schema(), Path(temp), 3)
            self.assertTrue(result["candidate_found"])
            self.assertEqual(result["checked"], 2)
            self.assertEqual(result["remote_updates_performed"], 0)
            self.assertTrue((Path(temp)/"candidate_document.json").exists())
        client.write.assert_not_called()

