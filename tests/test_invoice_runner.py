from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backfill_customer_branch import BackfillError, Store
from run_invoice_backfill import (update_one, reconcile_read_failure,
    reconcile_interrupted, reconcile_429, reconcile_omitted_field,
    retry_inactive_template_rejection, retry_dedicated_customfield_endpoint,
    reconcile_manual_branch_update, prepare_pending_continuation,
    should_process_invoice, recovery_candidates,
    PENDING_CONTINUATION_KEY, PENDING_CONTINUATION_MARKER,
    PENDING_CONTINUATION_160059_KEY, PENDING_CONTINUATION_160059_MARKER,
    PENDING_CONTINUATION_161528_KEY, PENDING_CONTINUATION_161528_MARKER,
    should_verify_investigated_invoice, reconcile_stock_only_difference,
    pending_continuation_batch_limit)
from backfill_customer_branch import document_differences
from repair_test_invoice import projection
from test_backfill import FakeOmittedClient, document, invoice_schema, row


class OmittedLookupClient:
    def __init__(self, r, value="", apply_write=True, reject_write=False):
        self.row = r
        self.doc = deepcopy(document(r))
        self.doc["custom_fields"] = [field for field in self.doc["custom_fields"]
                                     if str(field.get("customfield_id")) != "7"]
        self.doc["custom_field_hash"].pop("cf_customer_branch")
        self.lookup_value = value
        self.apply_write = apply_write
        self.reject_write = reject_write
        self.payloads = []
        self.reads = 0

    def read(self, row):
        self.reads += 1
        return deepcopy(self.doc)

    def request(self, method, path, payload=None):
        if method == "GET":
            field = deepcopy(document(self.row)["custom_fields"][0])
            field.update(data_type="lookup", value=self.lookup_value)
            return {"custom_fields": [field]}
        self.payloads.append((method, path, deepcopy(payload)))
        if self.reject_write:
            error = BackfillError("API rejected request (HTTP 404)")
            error.status = 404
            error.body = {"code": 1002, "message": "Inactive template cannot be used."}
            raise error
        if self.apply_write:
            self.lookup_value = payload["custom_fields"][0]["value"]
        return {"code": 0}


class StockReconciliationClient:
    def __init__(self, doc, lookup_value):
        self.doc = deepcopy(doc)
        self.lookup_value = lookup_value
        self.requests = []

    def read(self, row):
        return deepcopy(self.doc)

    def request(self, method, path, payload=None):
        self.requests.append((method, path, deepcopy(payload)))
        return {"custom_fields": [{
            "customfield_id": "1462401000020819910",
            "api_name": "cf_customer_branch",
            "data_type": "lookup",
            "value": self.lookup_value,
        }]}


class InvoiceRunnerTests(unittest.TestCase):
    def setUp(self):
        self.row = row()
        self.schema = invoice_schema()

    def client(self, mutate=None):
        client = FakeOmittedClient(self.row, mutate)
        client.payloads = []
        def request(method, path, payload=None):
            if method == "GET":
                field = next((f for f in client.doc.get("custom_fields", [])
                              if str(f.get("customfield_id")) == "7"),
                             document(self.row)["custom_fields"][0])
                return {"custom_fields": [{**field, "data_type": "lookup"}]}
            client.payloads.append((method, path, deepcopy(payload)))
            client.write(self.row, "7", client.doc)
            return {"code": 0}
        client.request = request
        return client

    def failed_429_store(self, folder, client, saved_original=None):
        recovery_row = {**self.row, "number": "156416"}
        original = deepcopy(client.doc if saved_original is None else saved_original)
        store = Store(Path(folder) / "run.sqlite3", [recovery_row], {})
        store.update(recovery_row, "failed", "API rejected request (HTTP 429)", original=original)
        store.halt("API rejected request (HTTP 429)")
        expected = {"method": "PUT", "path": "/invoices/" + recovery_row["zoho_id"], "payload": {
            "custom_fields": [{"customfield_id": self.schema["field_id"],
                               "value": recovery_row["branch_record_id"]}]}}
        store.record_evidence(recovery_row, "lookup_before", {
            "custom_fields": document(recovery_row)["custom_fields"][:1]})
        store.record_evidence(recovery_row, "request", expected)
        store.record_evidence(recovery_row, "write_error", {
            "error": "Uncertain write: temporary API failure; reconcile before retry",
            "http_status": None, "response": None})
        return store, recovery_row

    def failed_omitted_store(self, folder, client, current_value=""):
        recovery_row = {**self.row, "zoho_id": "1462401000002738299",
                        "number": "157368", "branch_record_id": "1462401000020819564"}
        original = deepcopy(client.doc)
        store = Store(Path(folder) / "run.sqlite3", [recovery_row], {})
        halt = "Customer Branch field absent or duplicated; run discovery"
        store.update(recovery_row, "failed", halt, original=original)
        store.halt(halt)
        field = deepcopy(document(recovery_row)["custom_fields"][0])
        field.update(data_type="lookup", value=current_value)
        store.record_evidence(recovery_row, "lookup_before", {"custom_fields": [field]})
        expected = {"method": "PUT", "path": "/invoices/" + recovery_row["zoho_id"],
                    "payload": {"custom_fields": [{"customfield_id": self.schema["field_id"],
                                                   "value": recovery_row["branch_record_id"]}]}}
        store.record_evidence(recovery_row, "request", expected)
        store.record_evidence(recovery_row, "write_error", {
            "error": "Uncertain write: network failure; read before retry",
            "http_status": None, "response": None})
        store.record_evidence(recovery_row, "readback", deepcopy(client.doc))
        return store, recovery_row

    def failed_template_rejection_store(self, folder, client):
        client.doc["template_id"] = "1462401000015911003"
        client.doc["template_name"] = "HQ Template"
        store, recovery_row = self.failed_omitted_store(folder, client)
        store.update(recovery_row, "failed", "Customer Branch update was not verified; halt retained")
        field = deepcopy(document(recovery_row)["custom_fields"][0])
        field.update(data_type="lookup", value="")
        body = {"custom_fields": [field]}
        expected = {"method": "PUT", "path": "/invoices/" + recovery_row["zoho_id"],
                    "payload": {"custom_fields": [{"customfield_id": self.schema["field_id"],
                                                   "value": recovery_row["branch_record_id"]}]}}
        store.record_evidence(recovery_row, "omitted_recovery_fresh_readback", deepcopy(client.doc))
        store.record_evidence(recovery_row, "omitted_recovery_fresh_lookup", body)
        store.record_evidence(recovery_row, "omitted_recovery_request", expected)
        store.record_evidence(recovery_row, "omitted_recovery_write_error", {
            "error": "API rejected request (HTTP 404)", "http_status": 404,
            "response": {"code": 1002, "message": "Inactive template cannot be used."}})
        store.record_evidence(recovery_row, "omitted_recovery_readback", deepcopy(client.doc))
        store.record_evidence(recovery_row, "omitted_recovery_verification_lookup", body)
        return store, recovery_row

    def failed_twice_template_rejection_store(self, folder, client):
        store, recovery_row = self.failed_template_rejection_store(folder, client)
        field = deepcopy(document(recovery_row)["custom_fields"][0])
        field.update(data_type="lookup", value="")
        body = {"custom_fields": [field]}
        expected = {"method": "PUT", "path": "/invoices/" + recovery_row["zoho_id"],
                    "payload": {"custom_fields": [{"customfield_id": self.schema["field_id"],
                                                   "value": recovery_row["branch_record_id"]}]}}
        store.record_evidence(recovery_row, "template_retry_fresh_readback", deepcopy(client.doc))
        store.record_evidence(recovery_row, "template_retry_fresh_lookup", body)
        store.record_evidence(recovery_row, "template_retry_request", expected)
        store.record_evidence(recovery_row, "template_retry_write_error", {
            "error": "API rejected request (HTTP 404)", "http_status": 404,
            "response": {"code": 1002, "message": "Inactive template cannot be used."}})
        store.record_evidence(recovery_row, "template_retry_readback", deepcopy(client.doc))
        store.record_evidence(recovery_row, "template_retry_verification_lookup", body)
        store.update(recovery_row, "failed",
                     "API rejected request (HTTP 404); branch remains unverified; halt retained")
        return store, recovery_row

    def failed_unauthorized_dedicated_store(self, folder, client):
        store, recovery_row = self.failed_twice_template_rejection_store(folder, client)
        field = deepcopy(document(recovery_row)["custom_fields"][0])
        field.update(data_type="lookup", value="")
        body = {"custom_fields": [field]}
        expected = {"method": "PUT",
                    "path": "/invoice/" + recovery_row["zoho_id"] + "/customfields",
                    "payload": {"custom_fields": [{"customfield_id": self.schema["field_id"],
                                                   "value": recovery_row["branch_record_id"]}]}}
        store.record_evidence(recovery_row, "customfield_endpoint_retry_fresh_readback",
                              deepcopy(client.doc))
        store.record_evidence(recovery_row, "customfield_endpoint_retry_fresh_lookup", body)
        store.record_evidence(recovery_row, "customfield_endpoint_retry_request", expected)
        store.record_evidence(recovery_row, "customfield_endpoint_retry_write_error", {
            "error": "API rejected request (HTTP 401)", "http_status": 401,
            "response": {"code": 57, "message": "You are not authorized to perform this operation"}})
        store.record_evidence(recovery_row, "customfield_endpoint_retry_readback",
                              deepcopy(client.doc))
        store.record_evidence(recovery_row, "customfield_endpoint_retry_verification_lookup", body)
        store.update(recovery_row, "failed",
                     "API rejected request (HTTP 401); branch remains unverified; halt retained")
        return store, recovery_row

    def stock_only_reconciliation_store(self, folder):
        field_id = "1462401000020819910"
        branch_id = "1462401000020818804"
        stock_row = {**self.row, "zoho_id": "1462401000002906040",
                     "number": "160059", "branch_record_id": branch_id}
        isolated_row = {**self.row, "zoho_id": "1462401000002738299",
                        "number": "157368", "branch_record_id": "1462401000020819564"}
        original = document(stock_row)
        original["custom_fields"][0]["customfield_id"] = field_id
        original["line_items"] = [deepcopy(original["line_items"][0]) for _ in range(5)]
        for key in ("actual_available_for_sale_stock", "actual_available_stock",
                    "available_for_sale_stock", "available_stock", "stock_on_hand"):
            original["line_items"][4][key] = 410.0
        saved_readback = deepcopy(original)
        saved_readback["custom_fields"][0]["value"] = branch_id
        saved_readback["custom_field_hash"]["cf_customer_branch"] = branch_id
        for key in ("actual_available_for_sale_stock", "actual_available_stock",
                    "available_for_sale_stock", "available_stock", "stock_on_hand"):
            saved_readback["line_items"][4][key] = 408.0

        schema = {**self.schema, "field_id": field_id}
        store = Store(Path(folder) / "run.sqlite3", [stock_row, isolated_row], {})
        store.update(stock_row, "failed",
                     "Other business details changed; stopped for investigation",
                     original=original)
        store.update(isolated_row, "failed",
                     "API rejected request (HTTP 401); branch remains unverified; halt retained",
                     original=document(isolated_row))
        store.halt("Other business details changed; stopped for investigation")
        store.put(PENDING_CONTINUATION_KEY, PENDING_CONTINUATION_MARKER)
        store.record_evidence(isolated_row, "customfield_endpoint_retry_request", {
            "method": "PUT", "path": "/invoice/" + isolated_row["zoho_id"] + "/customfields",
            "payload": {"custom_fields": [{"customfield_id": field_id,
                                            "value": isolated_row["branch_record_id"]}]}})
        store.record_evidence(isolated_row, "customfield_endpoint_retry_write_error", {
            "error": "API rejected request (HTTP 401)", "http_status": 401,
            "response": {"code": 57,
                         "message": "You are not authorized to perform this operation"}})
        store.record_evidence(stock_row, "lookup_before", {"custom_fields": [{
            "customfield_id": field_id, "api_name": "cf_customer_branch",
            "data_type": "lookup", "value": ""}]})
        store.record_evidence(stock_row, "request", {
            "method": "PUT", "path": "/invoices/" + stock_row["zoho_id"],
            "payload": {"custom_fields": [{"customfield_id": field_id,
                                            "value": branch_id}]}})
        store.record_evidence(stock_row, "write_response", {"code": 0})
        store.record_evidence(stock_row, "readback", saved_readback)
        store.record_evidence(stock_row, "business_differences", document_differences(
            projection(original, field_id, schema), projection(saved_readback, field_id, schema)))
        store.record_evidence(stock_row, "stock_only_reconciliation_readback", saved_readback)
        store.record_evidence(stock_row, "stock_only_reconciliation_differences", {
            "from_original": [{"path": "/contact/customer_balance"}],
            "from_saved_readback": [{"path": "/contact/customer_balance"}]})
        return store, stock_row, schema, original, saved_readback

    def add_unauthorized_161528_failure(self, store):
        field_id = "1462401000020819910"
        failed_row = {**self.row,
                      "zoho_id": PENDING_CONTINUATION_161528_MARKER["document_id"],
                      "number": PENDING_CONTINUATION_161528_MARKER["number"],
                      "branch_record_id": PENDING_CONTINUATION_161528_MARKER["branch_record_id"]}
        original = document(failed_row)
        original["custom_fields"][0].update(customfield_id=field_id, value="")
        original["custom_field_hash"]["cf_customer_branch"] = ""
        with store.db:
            store.db.execute("INSERT INTO documents(type,zoho_id,mapping) VALUES(?,?,?)",
                             (failed_row["type"], failed_row["zoho_id"], json.dumps(failed_row)))
        store.update(failed_row, "failed", PENDING_CONTINUATION_161528_MARKER["failure"],
                     original=original)
        store.record_evidence(failed_row, "request", {
            "method": "PUT", "path": "/invoices/" + failed_row["zoho_id"],
            "payload": {"custom_fields": [{"customfield_id": field_id,
                                            "value": failed_row["branch_record_id"]}]}})
        store.record_evidence(failed_row, "write_error", {
            "error": "API rejected request (HTTP 401)", "http_status": 401,
            "response": {"code": 57,
                         "message": "You are not authorized to perform this operation"}})
        lookup = {"custom_fields": [{"customfield_id": field_id,
                                    "api_name": "cf_customer_branch",
                                    "data_type": "lookup", "value": ""}]}
        store.record_evidence(failed_row, "lookup_before", lookup)
        store.record_evidence(failed_row, "readback", original)
        store.record_evidence(failed_row, "verification_lookup", lookup)
        store.halt(PENDING_CONTINUATION_161528_MARKER["halt_reason"])
        return failed_row

    def test_pending_continuation_preserves_the_known_failure_and_halt(self):
        with tempfile.TemporaryDirectory() as temp:
            recovery_row = {**self.row, "zoho_id": "1462401000002738299",
                            "number": "157368", "branch_record_id": "1462401000020819564"}
            store, _ = self.failed_unauthorized_dedicated_store(
                temp, OmittedLookupClient(recovery_row))
            try:
                item = store.db.execute(
                    "SELECT key,value FROM meta WHERE key LIKE ?",
                    ("evidence:invoice:" + recovery_row["zoho_id"] +
                     ":%:customfield_endpoint_retry_request",)).fetchone()
                request = json.loads(item["value"])
                request["payload"]["custom_fields"][0]["customfield_id"] = "1462401000020819910"
                store.put(item["key"], request)
                prepare_pending_continuation(store)
                self.assertEqual(store.get("halted"),
                                 "Customer Branch field absent or duplicated; run discovery")
                saved = store.db.execute(
                    "SELECT status,error FROM documents WHERE zoho_id=?",
                    (recovery_row["zoho_id"],)).fetchone()
                self.assertEqual(saved["status"], "failed")
                self.assertEqual(saved["error"],
                                 "API rejected request (HTTP 401); branch remains unverified; halt retained")
                self.assertEqual(store.get(PENDING_CONTINUATION_KEY)["document_id"],
                                 recovery_row["zoho_id"])
                store.put("halted", None)
                prepare_pending_continuation(store)
            finally:
                store.db.close()

    def test_pending_continuation_rejects_an_additional_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            recovery_row = {**self.row, "zoho_id": "1462401000002738299",
                            "number": "157368", "branch_record_id": "1462401000020819564"}
            store, _ = self.failed_unauthorized_dedicated_store(
                temp, OmittedLookupClient(recovery_row))
            other = {**self.row, "zoho_id": "another-invoice", "number": "OTHER"}
            with store.db:
                store.db.execute("INSERT INTO documents(type,zoho_id,mapping) VALUES(?,?,?)",
                                 (other["type"], other["zoho_id"], json.dumps(other)))
            store.update(other, "failed", "Another invoice failed")
            try:
                with self.assertRaises(BackfillError):
                    prepare_pending_continuation(store)
                self.assertIsNone(store.get(PENDING_CONTINUATION_KEY))
            finally:
                store.db.close()

    def test_explicit_160059_isolation_allows_pending_only_continuation(self):
        with tempfile.TemporaryDirectory() as temp:
            store, stock_row, _, _, _ = self.stock_only_reconciliation_store(temp)
            try:
                prepare_pending_continuation(store, isolate_stock_failure=True)
                self.assertIsNone(store.get("halted"))
                self.assertEqual(store.get(PENDING_CONTINUATION_160059_KEY),
                                 PENDING_CONTINUATION_160059_MARKER)
                self.assertEqual({
                    row["zoho_id"] for row in
                    store.db.execute("SELECT zoho_id FROM documents WHERE status='failed'")},
                    {PENDING_CONTINUATION_MARKER["document_id"], stock_row["zoho_id"]})
                self.assertEqual(
                    [row["zoho_id"] for row in recovery_candidates(store)], [])
                self.assertFalse(should_process_invoice("failed", True))
                self.assertTrue(should_process_invoice("pending", True))
                prepare_pending_continuation(store, isolate_stock_failure=True)
                prepare_pending_continuation(store)
            finally:
                store.db.close()

    def test_160059_isolation_requires_saved_additional_difference_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            store, stock_row, _, _, _ = self.stock_only_reconciliation_store(temp)
            with store.db:
                store.db.execute("DELETE FROM meta WHERE key LIKE ?",
                    ("evidence:invoice:" + stock_row["zoho_id"] +
                     ":%:stock_only_reconciliation_differences",))
            try:
                with self.assertRaisesRegex(BackfillError, "investigation does not prove"):
                    prepare_pending_continuation(store, isolate_stock_failure=True)
                self.assertEqual(store.get("halted"),
                                 "Other business details changed; stopped for investigation")
                self.assertIsNone(store.get(PENDING_CONTINUATION_160059_KEY))
            finally:
                store.db.close()

    def test_explicit_161528_isolation_requires_exact_401_and_allows_pending_only(self):
        with tempfile.TemporaryDirectory() as temp:
            store, _, _, _, _ = self.stock_only_reconciliation_store(temp)
            store.put(PENDING_CONTINUATION_160059_KEY,
                      PENDING_CONTINUATION_160059_MARKER)
            failed_row = self.add_unauthorized_161528_failure(store)
            try:
                prepare_pending_continuation(store, isolate_unauthorized_failure=True)
                self.assertIsNone(store.get("halted"))
                self.assertEqual(store.get(PENDING_CONTINUATION_161528_KEY),
                                 PENDING_CONTINUATION_161528_MARKER)
                self.assertFalse(should_process_invoice("failed", True))
                self.assertTrue(should_process_invoice("pending", True))
                self.assertEqual(
                    [row["zoho_id"] for row in recovery_candidates(store)], [])
                prepare_pending_continuation(store)
                saved = store.db.execute(
                    "SELECT status,error FROM documents WHERE zoho_id=?",
                    (failed_row["zoho_id"],)).fetchone()
                self.assertEqual(tuple(saved), (
                    "failed", PENDING_CONTINUATION_161528_MARKER["failure"]))
            finally:
                store.db.close()

    def test_161528_isolation_rejects_nonmatching_authorization_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            store, _, _, _, _ = self.stock_only_reconciliation_store(temp)
            store.put(PENDING_CONTINUATION_160059_KEY,
                      PENDING_CONTINUATION_160059_MARKER)
            failed_row = self.add_unauthorized_161528_failure(store)
            with store.db:
                store.db.execute(
                    "UPDATE meta SET value=? WHERE key LIKE ?",
                    (json.dumps({"error": "API rejected request (HTTP 403)",
                                 "http_status": 403, "response": {"code": 57}}),
                     "evidence:invoice:" + failed_row["zoho_id"] + ":%:write_error"))
            try:
                with self.assertRaisesRegex(BackfillError, "authorization evidence"):
                    prepare_pending_continuation(store, isolate_unauthorized_failure=True)
                self.assertEqual(store.get("halted"),
                                 PENDING_CONTINUATION_161528_MARKER["halt_reason"])
                self.assertIsNone(store.get(PENDING_CONTINUATION_161528_KEY))
            finally:
                store.db.close()

    def test_recovery_candidates_exclude_only_the_isolated_invoice(self):
        with tempfile.TemporaryDirectory() as temp:
            isolated = {**self.row, "zoho_id": "1462401000002738299",
                         "number": "157368", "branch_record_id": "1462401000020819564"}
            quota_failure = {**self.row, "zoho_id": "1462401000001974029",
                             "number": "156416"}
            store = Store(Path(temp)/"run.sqlite3", [isolated, quota_failure], {})
            try:
                store.update(isolated, "failed", PENDING_CONTINUATION_MARKER["failure"],
                             original=document(isolated))
                store.update(quota_failure, "failed", "API rejected request (HTTP 429)",
                             original=document(quota_failure))
                store.put(PENDING_CONTINUATION_KEY, PENDING_CONTINUATION_MARKER)
                failed = recovery_candidates(store)
                self.assertEqual([(row["type"], row["zoho_id"]) for row in failed],
                                 [("invoice", quota_failure["zoho_id"])])
            finally:
                store.db.close()

    def test_continue_pending_selects_only_pending_rows(self):
        self.assertTrue(should_process_invoice("pending", True))
        self.assertFalse(should_process_invoice("failed", True))
        self.assertFalse(should_process_invoice("updated_verified", True))
        self.assertTrue(should_process_invoice("failed", False))

    def test_pending_continuation_batch_stops_at_8000_completed(self):
        self.assertEqual(pending_continuation_batch_limit(6778, 7888), 1222)
        self.assertEqual(pending_continuation_batch_limit(6778, 100), 100)
        self.assertEqual(pending_continuation_batch_limit(8000, 7888), 0)
        self.assertEqual(pending_continuation_batch_limit(8001, 7888), 0)

    def test_existing_continuation_journal_does_not_depend_on_old_test_invoice(self):
        self.assertFalse(should_verify_investigated_invoice(True, False, True))
        self.assertTrue(should_verify_investigated_invoice(True, False, False))
        self.assertFalse(should_verify_investigated_invoice(False, True, True))
        self.assertTrue(should_verify_investigated_invoice(False, True, False))
        self.assertTrue(should_verify_investigated_invoice(False, False, True))

    def test_stock_only_reconciliation_verifies_branch_without_another_write(self):
        with tempfile.TemporaryDirectory() as temp:
            store, stock_row, schema, _, saved_readback = self.stock_only_reconciliation_store(temp)
            client = StockReconciliationClient(saved_readback, stock_row["branch_record_id"])
            try:
                reconcile_stock_only_difference(client, store, schema, Path(temp))
                self.assertEqual(client.requests, [(
                    "GET", "/invoices/" + stock_row["zoho_id"] + "/customfields", None)])
                status = store.db.execute("SELECT status,error FROM documents WHERE zoho_id=?",
                                          (stock_row["zoho_id"],)).fetchone()
                self.assertEqual(tuple(status), ("updated_verified", ""))
                self.assertIsNone(store.get("halted"))
                self.assertEqual(store.db.execute(
                    "SELECT status FROM documents WHERE zoho_id=?",
                    (PENDING_CONTINUATION_MARKER["document_id"],)).fetchone()[0], "failed")
            finally:
                store.db.close()

    def test_stock_only_reconciliation_keeps_halt_if_fresh_branch_is_wrong(self):
        with tempfile.TemporaryDirectory() as temp:
            store, stock_row, schema, _, saved_readback = self.stock_only_reconciliation_store(temp)
            fresh = deepcopy(saved_readback)
            fresh["custom_fields"][0]["value"] = ""
            fresh["custom_field_hash"]["cf_customer_branch"] = ""
            client = StockReconciliationClient(fresh, "")
            try:
                with self.assertRaises(BackfillError):
                    reconcile_stock_only_difference(client, store, schema, Path(temp))
                self.assertEqual(store.get("halted"),
                                 "Other business details changed; stopped for investigation")
                self.assertEqual(store.db.execute("SELECT status FROM documents WHERE zoho_id=?",
                                                  (stock_row["zoho_id"],)).fetchone()[0], "failed")
            finally:
                store.db.close()

    def test_stock_only_reconciliation_keeps_halt_on_other_fresh_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            store, stock_row, schema, _, saved_readback = self.stock_only_reconciliation_store(temp)
            fresh = deepcopy(saved_readback)
            fresh["total"] += 1
            client = StockReconciliationClient(fresh, stock_row["branch_record_id"])
            try:
                with self.assertRaises(BackfillError):
                    reconcile_stock_only_difference(client, store, schema, Path(temp))
                self.assertEqual(store.get("halted"),
                                 "Other business details changed; stopped for investigation")
                self.assertEqual(store.db.execute("SELECT status FROM documents WHERE zoho_id=?",
                                                  (stock_row["zoho_id"],)).fetchone()[0], "failed")
            finally:
                store.db.close()

    def test_branch_only_write_verified_and_original_is_unaltered(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp)/"run.sqlite3", [self.row], {})
            client = self.client()
            try:
                self.assertTrue(update_one(client, store, self.row, self.schema, True))
                self.assertEqual(client.payloads, [("PUT", "/invoices/001",
                    {"custom_fields": [{"customfield_id": "7", "value": "009"}]})])
                saved = store.db.execute("SELECT status,test_verified FROM documents").fetchone()
                self.assertEqual(tuple(saved), ("updated_verified", 1))
                with self.assertRaises(BackfillError):
                    update_one(client, store, self.row, self.schema)
                self.assertEqual(len(client.payloads), 1)
            finally:
                store.db.close()

    def test_omitted_full_readback_verifies_branch_via_dedicated_endpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp)/"run.sqlite3", [self.row], {})
            client = OmittedLookupClient(self.row)
            try:
                self.assertTrue(update_one(client, store, self.row, self.schema, True))
                self.assertEqual(client.payloads, [("PUT", "/invoices/001",
                    {"custom_fields": [{"customfield_id": "7", "value": "009"}]})])
                self.assertEqual(store.db.execute(
                    "SELECT status FROM documents").fetchone()[0], "updated_verified")
                self.assertEqual(store.db.execute(
                    "SELECT count(*) FROM meta WHERE key LIKE '%:verification_lookup'").fetchone()[0], 1)
            finally:
                store.db.close()

    def test_business_changes_block_success(self):
        for mutate in (lambda d: d.update(total=999),
                       lambda d: d["line_items"][0].update(quantity=7)):
            with tempfile.TemporaryDirectory() as temp:
                store = Store(Path(temp)/"run.sqlite3", [self.row], {})
                try:
                    with self.assertRaises(BackfillError):
                        update_one(self.client(mutate), store, self.row, self.schema, True)
                    self.assertEqual(store.db.execute("SELECT test_verified FROM documents").fetchone()[0], 0)
                finally:
                    store.db.close()

    def test_populated_unformatted_mirror_blocks_before_write(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp)/"run.sqlite3", [self.row], {})
            client = self.client()
            client.doc["custom_field_hash"]["cf_customer_branch_unformatted"] = "999"
            try:
                with self.assertRaises(BackfillError):
                    update_one(client, store, self.row, self.schema)
                self.assertEqual(client.payloads, [])
            finally:
                store.db.close()

    def test_recovery_verifies_without_repeating_write(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            store = Store(folder/"run.sqlite3", [self.row], {})
            client = self.client()
            try:
                update_one(client, store, self.row, self.schema, True)
                store.update(self.row, "failed", "Read failed after bounded retries")
                store.halt("Read failed after bounded retries")
                reconcile_read_failure(client, store, self.schema, folder)
                self.assertIsNone(store.get("halted"))
                self.assertEqual(len(client.payloads), 1)
                self.assertEqual(store.db.execute("SELECT status FROM documents").fetchone()[0], "updated_verified")
                self.assertEqual(len(list(folder.glob('before_recovery_*.sqlite3'))), 1)
            finally:
                store.db.close()

    def test_recovery_cannot_clear_business_difference_halt(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp)/"run.sqlite3", [self.row], {})
            try:
                store.halt("Other business details changed; stopped for investigation")
                with self.assertRaises(BackfillError):
                    reconcile_read_failure(self.client(), store, self.schema, Path(temp))
                self.assertTrue(store.get("halted"))
            finally:
                store.db.close()

    def test_429_recovery_retries_only_after_fresh_unchanged_blank_read(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            client = self.client()
            store, recovery_row = self.failed_429_store(folder, client)
            try:
                with patch("builtins.print"):
                    reconcile_429(client, store, self.schema, folder)
                self.assertEqual(client.payloads, [("PUT", "/invoices/001",
                    {"custom_fields": [{"customfield_id": "7", "value": "009"}]})])
                self.assertEqual(store.db.execute(
                    "SELECT status FROM documents WHERE type='invoice'").fetchone()[0], "updated_verified")
                self.assertIsNone(store.get("halted"))
                self.assertEqual(len(list(folder.glob("before_429_recovery_*.sqlite3"))), 1)
                recovery = store.db.execute(
                    "SELECT value FROM meta WHERE key LIKE 'http_429_recovery:%'").fetchone()
                self.assertEqual(json.loads(recovery[0])["remote_updates_performed"], 1)
            finally:
                store.db.close()

    def test_429_recovery_marks_already_applied_branch_without_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            recovery_row = {**self.row, "number": "156416"}
            original = document(recovery_row, "")
            client = self.client()
            client.doc = document(recovery_row, recovery_row["branch_record_id"])
            store, _ = self.failed_429_store(folder, client, original)
            try:
                with patch("builtins.print"):
                    reconcile_429(client, store, self.schema, folder)
                self.assertEqual(client.payloads, [])
                self.assertEqual(store.db.execute(
                    "SELECT status FROM documents WHERE type='invoice'").fetchone()[0], "updated_verified")
                self.assertIsNone(store.get("halted"))
                recovery = store.db.execute(
                    "SELECT value FROM meta WHERE key LIKE 'http_429_recovery:%'").fetchone()
                self.assertEqual(json.loads(recovery[0])["remote_updates_performed"], 0)
            finally:
                store.db.close()

    def test_429_recovery_keeps_halt_on_business_change_or_branch_conflict(self):
        def branch_conflict(doc):
            doc["custom_fields"].insert(0, document(self.row, "other")["custom_fields"][0])
            doc["custom_field_hash"]["cf_customer_branch"] = "other"

        for mutate in (lambda doc: doc.update(total=999), branch_conflict):
            with tempfile.TemporaryDirectory() as temp:
                folder = Path(temp)
                client = self.client()
                store, _ = self.failed_429_store(folder, client)
                mutate(client.doc)
                try:
                    with patch("builtins.print"), self.assertRaises(BackfillError):
                        reconcile_429(client, store, self.schema, folder)
                    self.assertTrue(store.get("halted"))
                    self.assertEqual(client.payloads, [])
                    self.assertEqual(store.db.execute(
                        "SELECT status FROM documents WHERE type='invoice'").fetchone()[0], "failed")
                finally:
                    store.db.close()

    def test_429_recovery_rejects_ambiguous_evidence_before_api_read(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            client = self.client()
            store, _ = self.failed_429_store(folder, client)
            store.record_evidence(self.row, "write_response", {"code": 0})
            try:
                with self.assertRaises(BackfillError):
                    reconcile_429(client, store, self.schema, folder)
                self.assertEqual(client.reads, 0)
                self.assertTrue(store.get("halted"))
            finally:
                store.db.close()

    def test_omitted_field_recovery_retries_once_and_verifies(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            recovery_row = {**self.row, "zoho_id": "1462401000002738299",
                            "number": "157368", "branch_record_id": "1462401000020819564"}
            client = OmittedLookupClient(recovery_row)
            store, _ = self.failed_omitted_store(folder, client)
            try:
                with patch("builtins.print"):
                    reconcile_omitted_field(client, store, self.schema, folder)
                self.assertEqual(client.payloads, [("PUT", "/invoices/1462401000002738299",
                    {"custom_fields": [{"customfield_id": "7", "value": "1462401000020819564"}]})])
                self.assertEqual(store.db.execute(
                    "SELECT status FROM documents").fetchone()[0], "updated_verified")
                self.assertIsNone(store.get("halted"))
                self.assertEqual(len(list(folder.glob("before_omitted_field_recovery_*.sqlite3"))), 1)
                recovery = store.db.execute(
                    "SELECT value FROM meta WHERE key LIKE 'omitted_field_recovery:%'").fetchone()
                self.assertEqual(json.loads(recovery[0])["remote_updates_performed"], 1)
            finally:
                store.db.close()

    def test_omitted_field_recovery_does_not_retry_if_branch_already_applied(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            recovery_row = {**self.row, "zoho_id": "1462401000002738299",
                            "number": "157368", "branch_record_id": "1462401000020819564"}
            client = OmittedLookupClient(recovery_row, recovery_row["branch_record_id"])
            store, _ = self.failed_omitted_store(folder, client)
            try:
                with patch("builtins.print"):
                    reconcile_omitted_field(client, store, self.schema, folder)
                self.assertEqual(client.payloads, [])
                self.assertEqual(store.db.execute(
                    "SELECT status FROM documents").fetchone()[0], "updated_verified")
                self.assertIsNone(store.get("halted"))
            finally:
                store.db.close()

    def test_omitted_field_recovery_keeps_halt_if_retry_is_not_verified(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            recovery_row = {**self.row, "zoho_id": "1462401000002738299",
                            "number": "157368", "branch_record_id": "1462401000020819564"}
            client = OmittedLookupClient(recovery_row, apply_write=False)
            store, _ = self.failed_omitted_store(folder, client)
            try:
                with patch("builtins.print"), self.assertRaises(BackfillError):
                    reconcile_omitted_field(client, store, self.schema, folder)
                self.assertEqual(len(client.payloads), 1)
                self.assertTrue(store.get("halted"))
                self.assertEqual(store.db.execute(
                    "SELECT status FROM documents").fetchone()[0], "failed")
            finally:
                store.db.close()

    def test_omitted_field_recovery_keeps_halt_on_change_or_branch_conflict(self):
        for conflict in ("business", "branch"):
            with self.subTest(conflict=conflict), tempfile.TemporaryDirectory() as temp:
                folder = Path(temp)
                recovery_row = {**self.row, "zoho_id": "1462401000002738299",
                                "number": "157368", "branch_record_id": "1462401000020819564"}
                client = OmittedLookupClient(recovery_row)
                store, _ = self.failed_omitted_store(folder, client)
                if conflict == "business":
                    client.doc["total"] = 999
                else:
                    client.lookup_value = "different"
                try:
                    with patch("builtins.print"), self.assertRaises(BackfillError):
                        reconcile_omitted_field(client, store, self.schema, folder)
                    self.assertTrue(store.get("halted"))
                    self.assertEqual(client.payloads, [])
                    self.assertEqual(store.db.execute(
                        "SELECT status FROM documents").fetchone()[0], "failed")
                finally:
                    store.db.close()

    def test_omitted_field_recovery_rejects_ambiguous_evidence_before_read(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            recovery_row = {**self.row, "zoho_id": "1462401000002738299",
                            "number": "157368", "branch_record_id": "1462401000020819564"}
            client = OmittedLookupClient(recovery_row)
            store, _ = self.failed_omitted_store(folder, client)
            store.record_evidence(recovery_row, "write_response", {"code": 0})
            try:
                with self.assertRaises(BackfillError):
                    reconcile_omitted_field(client, store, self.schema, folder)
                self.assertEqual(client.reads, 0)
                self.assertTrue(store.get("halted"))
                self.assertEqual(client.payloads, [])
            finally:
                store.db.close()

    def test_inactive_template_retry_succeeds_after_exact_saved_rejection(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            recovery_row = {**self.row, "zoho_id": "1462401000002738299",
                            "number": "157368", "branch_record_id": "1462401000020819564"}
            client = OmittedLookupClient(recovery_row)
            store, _ = self.failed_template_rejection_store(folder, client)
            try:
                with patch("builtins.print"):
                    retry_inactive_template_rejection(client, store, self.schema, folder, True)
                self.assertEqual(len(client.payloads), 1)
                self.assertEqual(client.payloads[0][0:2],
                                 ("PUT", "/invoices/1462401000002738299"))
                self.assertEqual(store.db.execute(
                    "SELECT status FROM documents").fetchone()[0], "updated_verified")
                self.assertIsNone(store.get("halted"))
            finally:
                store.db.close()

    def test_inactive_template_retry_requires_confirmation_and_preserves_halt_on_rejection(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            recovery_row = {**self.row, "zoho_id": "1462401000002738299",
                            "number": "157368", "branch_record_id": "1462401000020819564"}
            client = OmittedLookupClient(recovery_row, reject_write=True)
            store, _ = self.failed_template_rejection_store(folder, client)
            try:
                with self.assertRaises(BackfillError):
                    retry_inactive_template_rejection(client, store, self.schema, folder, False)
                self.assertEqual(client.reads, 0)
                with patch("builtins.print"), self.assertRaises(BackfillError):
                    retry_inactive_template_rejection(client, store, self.schema, folder, True)
                self.assertEqual(len(client.payloads), 1)
                self.assertTrue(store.get("halted"))
                self.assertEqual(store.db.execute(
                    "SELECT status FROM documents").fetchone()[0], "failed")
            finally:
                store.db.close()

    def test_inactive_template_retry_blocks_if_invoice_changed_before_write(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            recovery_row = {**self.row, "zoho_id": "1462401000002738299",
                            "number": "157368", "branch_record_id": "1462401000020819564"}
            client = OmittedLookupClient(recovery_row)
            store, _ = self.failed_template_rejection_store(folder, client)
            client.doc["total"] = 999
            try:
                with self.assertRaises(BackfillError):
                    retry_inactive_template_rejection(client, store, self.schema, folder, True)
                self.assertEqual(client.payloads, [])
                self.assertTrue(store.get("halted"))
            finally:
                store.db.close()

    def test_dedicated_customfield_retry_uses_documented_field_only_endpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            recovery_row = {**self.row, "zoho_id": "1462401000002738299",
                            "number": "157368", "branch_record_id": "1462401000020819564"}
            client = OmittedLookupClient(recovery_row)
            store, _ = self.failed_twice_template_rejection_store(folder, client)
            try:
                with patch("builtins.print"):
                    retry_dedicated_customfield_endpoint(client, store, self.schema, folder)
                self.assertEqual(client.payloads, [("PUT", "/invoice/1462401000002738299/customfields",
                    {"custom_fields": [{"customfield_id": "7", "value": "1462401000020819564"}]})])
                self.assertEqual(store.db.execute(
                    "SELECT status FROM documents").fetchone()[0], "updated_verified")
                self.assertIsNone(store.get("halted"))
            finally:
                store.db.close()

    def test_dedicated_customfield_retry_retains_halt_and_cannot_repeat_if_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            recovery_row = {**self.row, "zoho_id": "1462401000002738299",
                            "number": "157368", "branch_record_id": "1462401000020819564"}
            client = OmittedLookupClient(recovery_row, reject_write=True)
            store, _ = self.failed_twice_template_rejection_store(folder, client)
            try:
                with patch("builtins.print"), self.assertRaises(BackfillError):
                    retry_dedicated_customfield_endpoint(client, store, self.schema, folder)
                self.assertEqual(len(client.payloads), 1)
                self.assertTrue(store.get("halted"))
                self.assertEqual(store.db.execute(
                    "SELECT status FROM documents").fetchone()[0], "failed")
                with self.assertRaises(BackfillError):
                    retry_dedicated_customfield_endpoint(client, store, self.schema, folder)
                self.assertEqual(len(client.payloads), 1)
            finally:
                store.db.close()

    def test_manual_branch_reconciliation_clears_halt_without_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            recovery_row = {**self.row, "zoho_id": "1462401000002738299",
                            "number": "157368", "branch_record_id": "1462401000020819564"}
            client = OmittedLookupClient(recovery_row, recovery_row["branch_record_id"])
            store, _ = self.failed_unauthorized_dedicated_store(folder, client)
            try:
                with patch("builtins.print"):
                    reconcile_manual_branch_update(client, store, self.schema, folder)
                self.assertEqual(client.payloads, [])
                self.assertEqual(store.db.execute(
                    "SELECT status FROM documents").fetchone()[0], "updated_verified")
                self.assertIsNone(store.get("halted"))
                self.assertEqual(len(list(folder.glob("before_manual_branch_reconciliation_*.sqlite3"))), 1)
            finally:
                store.db.close()

    def test_manual_branch_reconciliation_keeps_halt_if_branch_is_still_blank(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            recovery_row = {**self.row, "zoho_id": "1462401000002738299",
                            "number": "157368", "branch_record_id": "1462401000020819564"}
            client = OmittedLookupClient(recovery_row)
            store, _ = self.failed_unauthorized_dedicated_store(folder, client)
            try:
                with self.assertRaises(BackfillError):
                    reconcile_manual_branch_update(client, store, self.schema, folder)
                self.assertEqual(client.payloads, [])
                self.assertTrue(store.get("halted"))
                self.assertEqual(store.db.execute(
                    "SELECT status FROM documents").fetchone()[0], "failed")
            finally:
                store.db.close()

    def test_manual_branch_reconciliation_can_be_retried_after_a_read_only_blank_check(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            recovery_row = {**self.row, "zoho_id": "1462401000002738299",
                            "number": "157368", "branch_record_id": "1462401000020819564"}
            client = OmittedLookupClient(recovery_row)
            store, _ = self.failed_unauthorized_dedicated_store(folder, client)
            try:
                with self.assertRaises(BackfillError):
                    reconcile_manual_branch_update(client, store, self.schema, folder)
                self.assertTrue(store.get("halted"))
                client.lookup_value = recovery_row["branch_record_id"]
                with patch("builtins.print"):
                    reconcile_manual_branch_update(client, store, self.schema, folder)
                self.assertEqual(client.payloads, [])
                self.assertIsNone(store.get("halted"))
                self.assertEqual(store.db.execute(
                    "SELECT status FROM documents").fetchone()[0], "updated_verified")
            finally:
                store.db.close()

    def test_interruption_reconciles_saved_write_without_resending(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp)/"run.sqlite3", [self.row], {})
            client = self.client()
            try:
                update_one(client, store, self.row, self.schema)
                store.update(self.row, "updating")
                reconcile_interrupted(client, store, self.schema)
                self.assertEqual(len(client.payloads), 1)
                self.assertEqual(store.db.execute("SELECT status FROM documents").fetchone()[0], 'updated_verified')
            finally:
                store.db.close()

    def test_interruption_with_uncertain_blank_request_cannot_resend(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp)/"run.sqlite3", [self.row], {})
            client = self.client()
            try:
                store.update(self.row, 'updating', original=client.doc)
                store.record_evidence(self.row, 'request', {'method': 'PUT'})
                with self.assertRaises(BackfillError):
                    reconcile_interrupted(client, store, self.schema)
                self.assertEqual(client.payloads, [])
                self.assertEqual(store.db.execute('SELECT status FROM documents').fetchone()[0], 'updating')
            finally:
                store.db.close()
