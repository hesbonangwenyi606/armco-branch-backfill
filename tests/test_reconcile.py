import csv
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import reconcile_map as r
from backfill_customer_branch import BackfillError


def source(number="152522"):
    return {"type": "invoice", "zoho_id": "1462401000001974016", "number": number,
            "branch_name": "Branch A", "branch_record_id": "001"}


def invoice(identifier="1462401000001974003", number="152522"):
    return {"invoice_id": identifier, "invoice_number": number, "customer_id": "10",
            "customer_name": "Parent", "date": "2025-02-04"}


class ReconciliationTests(unittest.TestCase):
    def test_source_accepts_damaged_ids_but_rejects_duplicate_numbers(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "source.csv"
            def write(rows):
                with path.open("w", newline="", encoding="utf-8") as file:
                    writer = csv.DictWriter(file, fieldnames=r.COLUMNS)
                    writer.writeheader()
                    writer.writerows(rows)
            write([source(), source("152550")])
            rows, fingerprint = r.source_map(path, {"invoice": 2})
            self.assertEqual(len(rows), 2)
            self.assertEqual(len(fingerprint), 64)
            write([source(), source()])
            with self.assertRaisesRegex(BackfillError, "Duplicate document number"):
                r.source_map(path, {"invoice": 2})

    def test_exact_number_matching_preserves_exclusions_and_long_ids(self):
        index = r.document_index([invoice(), invoice(number="152522-extra"), invoice(number="excluded")],
                                 "invoice", {"152522"})
        self.assertEqual(set(index), {"152522"})
        self.assertEqual(list(index["152522"]), ["1462401000001974003"])

    def test_ambiguous_missing_and_unverified_records_block_candidate(self):
        index = r.document_index([invoice(), invoice("99")], "invoice", {"152522"})
        reports, candidate = r.reconcile([source(), source("missing")], {"invoice": index})
        self.assertEqual([i["document_status"] for i in reports], ["document_ambiguous", "document_missing"])
        self.assertTrue(all(i["branch_status"] == "branch_unverified" for i in reports))
        self.assertEqual(candidate, [])

    def test_branch_matching_requires_exact_unique_name_and_resolves_both_ids(self):
        docs = r.document_index([invoice()], "invoice", {"152522"})
        branches = r.branch_index([{"record_name": "Branch A", "module_record_id": "0009"}], "record_name")
        reports, candidate = r.reconcile([source()], {"invoice": docs}, branches)
        self.assertEqual(candidate[0]["zoho_id"], "1462401000001974003")
        self.assertEqual(candidate[0]["branch_record_id"], "0009")
        self.assertEqual(candidate[0]["branch_name"], "Branch A")
        self.assertEqual(reports[0]["source_zoho_id"], source()["zoho_id"])
        for branches, expected in (({"branch a": {"9"}}, "branch_missing"),
                                   ({"Branch A": {"9", "10"}}, "branch_ambiguous")):
            reports, candidate = r.reconcile([source()], {"invoice": docs}, branches)
            self.assertEqual(reports[0]["branch_status"], expected)
            self.assertEqual(candidate, [])

    def test_conflicting_duplicate_api_document_fails(self):
        with self.assertRaisesRegex(BackfillError, "changed between"):
            r.document_index([invoice(), {**invoice(), "customer_id": "11"}], "invoice", {"152522"})

    def test_discovery_uses_metadata_not_guessed_module_name(self):
        client = Mock()
        client.request.return_value = {"modules": [{"module_name": "Customer Branch",
                                                    "module_api_name": "cm_customer_branch"}]}
        self.assertEqual(r.discover_branch_module(client), "cm_customer_branch")
        client.request.assert_called_once_with("GET", "/settings/modules")
        client.request.return_value["modules"].append({"module_name": "Customer Branches",
                                                       "module_api_name": "cm_other"})
        with self.assertRaises(BackfillError):
            r.discover_branch_module(client)

    def test_write_prohibited_before_any_authentication(self):
        client = r.ReadOnlyClient({})
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            with self.assertRaisesRegex(BackfillError, "GET requests only"):
                client.request(method, "/invoices/1", {})

    def test_paginated_scan_resumes_from_validated_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = r.PageCache(Path(folder) / "cache.sqlite3", {"org": "1"})
            self.addCleanup(cache.db.close)
            client = Mock()
            client.request.side_effect = [
                {"invoices": [invoice()], "page_context": {"page": 1, "has_more_page": True}},
                BackfillError("Network failure"),
                {"invoices": [invoice("22", "152550")], "page_context": {"page": 2, "has_more_page": False}}]
            with patch("builtins.print"):
                with self.assertRaises(BackfillError):
                    list(r.scan(client, cache, "/invoices", "invoices"))
                items = list(r.scan(client, cache, "/invoices", "invoices"))
            self.assertEqual(len(items), 2)
            self.assertEqual(client.request.call_count, 3)
            for call in client.request.call_args_list:
                self.assertEqual(call.args[0], "GET")
            cache.db.close()

    def test_invalid_pagination_never_assumes_complete(self):
        for body in ({"invoices": [], "page_context": {}},
                     {"invoices": [], "page_context": {"page": 1, "has_more_page": True}},
                     {"invoices": [], "page_context": {"page": 2, "has_more_page": False}}):
            with self.subTest(body=body):
                cache = Mock()
                cache.page.return_value = body
                with self.assertRaises(BackfillError):
                    list(r.scan(Mock(), cache, "/invoices", "invoices"))
                cache.save.assert_not_called()

    def test_cache_identity_rejects_different_map_or_organisation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cache.sqlite3"
            cache = r.PageCache(path, {"org": "1", "sha256": "abc"})
            cache.db.close()
            for identity in ({"org": "2", "sha256": "abc"}, {"org": "1", "sha256": "changed"}):
                with self.assertRaisesRegex(BackfillError, "identity changed"):
                    r.PageCache(path, identity)

    def test_documents_only_cli_never_publishes_candidate(self):
        with tempfile.TemporaryDirectory() as folder:
            client = Mock()
            client.request.side_effect = [
                {"invoices": [invoice()], "page_context": {"page": 1, "has_more_page": False}},
                {"creditnotes": [], "page_context": {"page": 1, "has_more_page": False}}]
            with patch.object(r, "source_map", return_value=([source()], "abc")), \
                 patch.object(r, "settings", return_value={"ZOHO_ORG_ID": "1", "ZOHO_ACCOUNTS": "accounts", "ZOHO_API": "api"}), \
                 patch.object(r, "ReadOnlyClient", return_value=client), patch("builtins.print"):
                self.assertEqual(r.main(["--output", folder]), 0)
            self.assertEqual(list(Path(folder).rglob("candidate_map.csv")), [])
            report = list(Path(folder).rglob("document_matches.csv"))
            self.assertEqual(len(report), 1)
            with report[0].open(newline="", encoding="utf-8") as file:
                saved = list(csv.DictReader(file))
            self.assertEqual(saved[0]["resolved_zoho_id"], "1462401000001974003")
            self.assertEqual(saved[0]["branch_status"], "branch_unverified")


if __name__ == "__main__":
    unittest.main()
