import copy
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

import backfill_customer_branch as b


def row(t="invoice", identifier="001"):
    return dict(type=t, zoho_id=identifier, number="DOC-1", branch_name="Branch A", branch_record_id="009")


def document(r, value=""):
    return {r["type"] + "_id": r["zoho_id"], r["type"] + "_number": r["number"],
            "customer_id": "123", "total": 100, "status": "sent", "balance": 50,
            "line_items": [{"item_id": "20", "quantity": 1, "rate": 100}],
            "custom_fields": [{"customfield_id": "7", "index": 2, "api_name": "cf_customer_branch", "value": value},
                              {"customfield_id": "8", "value": "FISCAL-1"}],
            "custom_field_hash": {"cf_customer_branch": value, "cf_fiscal": "FISCAL-1"}}


def invoice_schema():
    return {"field_id": "7", "api_name": "cf_customer_branch", "data_type": "lookup",
            "entity": "invoice", "related_entity": "cm_customer_branch", "permission": "write",
            "visibility": "all", "is_active": True, "is_read_only": False,
            "is_custom_field": True, "index": 2}


class FakeClient:
    def __init__(self, r, value="", mutate=None, uncertain=False):
        self.doc = document(r, value)
        self.mutate, self.uncertain, self.writes, self.reads = mutate, uncertain, 0, 0

    def read(self, r):
        self.reads += 1
        return copy.deepcopy(self.doc)

    def write(self, r, field, before):
        self.writes += 1
        self.doc["custom_fields"][0]["value"] = r["branch_record_id"]
        self.doc["custom_field_hash"]["cf_customer_branch"] = r["branch_record_id"]
        if self.mutate:
            self.mutate(self.doc)
        if self.uncertain:
            raise b.BackfillError("Uncertain write")


class FakeOmittedClient(FakeClient):
    def __init__(self, r, mutate=None):
        super().__init__(r, mutate=mutate)
        self.doc["custom_fields"].pop(0)
        self.doc["custom_field_hash"].pop("cf_customer_branch")

    def write(self, r, field, before):
        self.writes += 1
        entry = document(r, r["branch_record_id"])["custom_fields"][0]
        entry["index"] = 1  # Observed document index differs from schema index.
        self.doc["custom_fields"].insert(0, entry)
        self.doc["custom_field_hash"]["cf_customer_branch"] = r["branch_record_id"]
        if self.mutate:
            self.mutate(self.doc)


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "map.csv"

    def write(self, rows, fields=b.COLUMNS):
        with self.path.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def test_ids_remain_text_and_fingerprint_changes(self):
        self.write([row()])
        rows, fingerprint = b.load_map(self.path, {"invoice": 1})
        self.assertEqual(rows[0]["zoho_id"], "001")
        self.assertEqual(rows[0]["branch_record_id"], "009")
        self.path.write_bytes(self.path.read_bytes() + b"\n")
        self.assertNotEqual(fingerprint, b.load_map(self.path, {"invoice": 1})[1])

    def test_duplicate_conflict_invalid_and_wrong_scope_rejected(self):
        for rows in ([row(), row()], [row(), {**row(), "branch_record_id": "99"}],
                     [{**row(), "type": "bill"}], [{**row(), "zoho_id": "1e10"}],
                     [{**row(), "branch_record_id": ""}]):
            with self.subTest(rows=rows):
                self.write(rows)
                with self.assertRaises(b.BackfillError):
                    b.load_map(self.path, {"invoice": 1})
        self.write([row()])
        with self.assertRaises(b.BackfillError):
            b.load_map(self.path)

    def test_missing_columns(self):
        self.path.write_text("type,zoho_id\ninvoice,001\n")
        with self.assertRaises(b.BackfillError):
            b.load_map(self.path)


class ProcessingTests(unittest.TestCase):
    def test_write_rejection_evidence_survives_later_verification_failure(self):
        r = self.rows[0]
        client = FakeClient(r)
        before = client.read(r)
        client.read = Mock(side_effect=[before, {**before, "total": 999}])
        client.write = Mock(side_effect=b.APIError(400, {"code": 123, "message": "PRIVATE VENDOR RESPONSE"}))
        with self.assertRaises(b.VerificationError):
            b.process(client, self.store, [r], self.config)
        entries = [(key, json.loads(value)) for key, value in self.store.db.execute(
            "SELECT key,value FROM meta WHERE key LIKE 'evidence:%'")]
        rejection = next(value for key, value in entries if key.endswith(":write_error"))
        readback = next(value for key, value in entries if key.endswith(":readback"))
        self.assertEqual(rejection["http_status"], 400)
        self.assertEqual(rejection["response_body"]["code"], 123)
        self.assertNotIn("PRIVATE", rejection["error"])
        self.assertEqual(readback["total"], 999)
        self.assertTrue(self.store.get("halted"))

    def test_successful_write_response_and_readback_are_saved(self):
        r = self.rows[0]
        client = FakeClient(r)
        b.process(client, self.store, [r], self.config)
        entries = list(self.store.db.execute("SELECT key FROM meta WHERE key LIKE 'evidence:%'"))
        self.assertTrue(any(key.endswith(":write_response") for (key,) in entries))
        self.assertTrue(any(key.endswith(":readback") for (key,) in entries))

    def test_audit_does_not_discard_differences_when_lookup_is_still_omitted(self):
        r = self.rows[0]
        identity = {**self.identity, "CF_INVOICE_ID": "7"}
        original = FakeOmittedClient(r).doc
        original["invoice_url"] = "PRIVATE OLD URL"
        self.store.update(r, "failed", "Verification stopped", original=original)
        self.store.halt("Verification stopped")
        with self.store.db:
            self.store.put("identity", identity)
            self.store.put("invoice_ui_empty_confirmation", {"document_id": r["zoho_id"], "schema": invoice_schema()})
        client = FakeOmittedClient(r)
        client.doc["invoice_url"] = "PRIVATE NEW URL"
        with patch("builtins.print") as printed:
            b.audit_saved_document(client, self.folder, "invoice", r["zoho_id"], identity)
        summary = json.loads(printed.call_args.args[0])
        self.assertEqual(summary["protected_changed_paths"], ["/invoice_url"])
        self.assertIsNone(summary["expected_branch_matches"])
        self.assertIsNone(summary["normalization_error"])
        self.assertTrue(summary["branch_read_error"])
        self.assertNotIn("PRIVATE", printed.call_args.args[0])
        self.assertTrue(self.store.get("halted"))
        self.assertEqual(client.writes, 0)

    def test_halted_document_audit_keeps_halt_and_reports_paths_without_console_values(self):
        r = self.rows[0]
        identity = {**self.identity, "CF_INVOICE_ID": "7"}
        original = FakeOmittedClient(r).doc
        self.store.update(r, "failed", "Protected data changed", original=original)
        self.store.halt("Protected data changed")
        with self.store.db:
            self.store.put("identity", identity)
            self.store.put("invoice_ui_empty_confirmation", {"document_id": r["zoho_id"], "schema": invoice_schema()})
        client = FakeClient(r, r["branch_record_id"])
        client.doc["last_modified_by_id"] = "PRIVATE OPERATOR"
        with patch("builtins.print") as printed:
            b.audit_saved_document(client, self.folder, "invoice", r["zoho_id"], identity)
        summary = json.loads(printed.call_args.args[0])
        self.assertTrue(summary["expected_branch_matches"])
        self.assertEqual(summary["protected_changed_paths"], ["/last_modified_by_id"])
        self.assertNotIn("PRIVATE", printed.call_args.args[0])
        self.assertEqual(client.writes, 0)
        self.assertEqual(self.status(r)["status"], "failed")
        self.assertEqual(self.store.get("halted"), "Protected data changed")
        self.assertEqual(json.loads(self.status(r)["original"]), original)
        evidence = Path(summary["evidence_folder"])
        self.assertEqual(json.loads((evidence / "original.json").read_text(encoding="utf-8")), original)
        self.assertEqual(json.loads((evidence / "current.json").read_text(encoding="utf-8")), client.doc)

    def test_audit_rejects_identity_mismatch_before_api_read(self):
        client = Mock()
        with self.assertRaisesRegex(b.BackfillError, "identity"):
            b.audit_saved_document(client, self.folder, "invoice", self.rows[0]["zoho_id"], {"wrong": True})
        client.read.assert_not_called()

    def test_ui_confirmed_invoice_test_preserves_actual_original_and_verifies_by_id(self):
        r = self.rows[0]
        client = FakeOmittedClient(r)
        self.assertEqual(b.process(client, self.store, [r], self.config, test=True, limit=1,
                                  omitted_test_schema=invoice_schema()), 1)
        saved = self.status(r)
        self.assertEqual(saved["status"], "updated_verified")
        self.assertEqual(saved["test_verified"], 1)
        original = json.loads(saved["original"])
        self.assertEqual([f["customfield_id"] for f in original["custom_fields"]], ["8"])
        self.assertEqual(client.writes, 1)

    def test_ui_confirmation_never_enables_full_run_or_creditnote_writes(self):
        client = Mock()
        for rows, options in (([self.rows[0]], {}), ([self.rows[0]], {"test": True}),
                              (self.rows, {"test": True, "limit": 1}),
                              ([self.rows[1]], {"test": True, "limit": 1}),
                              ([self.rows[0]], {"test": True, "limit": 1, "dry": True})):
            with self.subTest(options=options), self.assertRaises(b.BackfillError):
                b.process(client, self.store, rows, self.config, omitted_test_schema=invoice_schema(), **options)
        client.read.assert_not_called()
        client.write.assert_not_called()

    def test_ui_confirmed_test_halts_on_missing_readback_or_fiscal_change(self):
        r = self.rows[0]
        for change in (lambda d: d["custom_fields"][0].update(value=""),
                       lambda d: d["custom_fields"][1].update(value="CHANGED")):
            with self.subTest(change=change):
                with self.store.db:
                    self.store.put("halted", None)
                    self.store.db.execute("UPDATE documents SET status='pending',original=NULL")
                client = FakeOmittedClient(r, mutate=change)
                with self.assertRaises(b.VerificationError):
                    b.process(client, self.store, [r], self.config, test=True, limit=1,
                              omitted_test_schema=invoice_schema())
                self.assertTrue(self.store.get("halted"))
                self.assertEqual(client.writes, 1)

    def test_ui_confirmed_test_restart_reconciles_before_another_write(self):
        r = self.rows[0]
        original = FakeOmittedClient(r).doc
        self.store.update(r, "updating", original=original)
        client = FakeClient(r, value=r["branch_record_id"])
        self.assertEqual(b.process(client, self.store, [r], self.config, test=True, limit=1,
                                  omitted_test_schema=invoice_schema()), 1)
        self.assertEqual(client.writes, 0)
        self.assertEqual(json.loads(self.status(r)["original"]), original)

    def test_missing_value_or_nonempty_mirror_cannot_be_treated_as_empty(self):
        r = self.rows[0]
        doc = document(r)
        del doc["custom_fields"][0]["value"]
        with self.assertRaisesRegex(b.BackfillError, "lacks a value"):
            b.branch_value(doc, "7", invoice_schema())
        doc = FakeOmittedClient(r).doc
        doc["custom_field_hash"]["cf_customer_branch"] = "123"
        with self.assertRaises(b.BackfillError):
            b.branch_value(doc, "7", invoice_schema())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.rows = [row(), row("creditnote", "002")]
        self.identity = {"sha256": "abc", "ZOHO_ORG_ID": "1"}
        self.store = b.Store(self.folder / "progress.sqlite3", self.rows, self.identity)
        self.addCleanup(self.store.db.close)
        self.config = {"CF_INVOICE_ID": "7", "CF_CREDITNOTE_ID": "7"}

    def status(self, r):
        return self.store.db.execute("SELECT * FROM documents WHERE type=? AND zoho_id=?",
                                     (r["type"], r["zoho_id"])).fetchone()

    def test_success_both_types_and_no_duplicate_write_on_restart(self):
        for r in self.rows:
            client = FakeClient(r)
            b.process(client, self.store, [r], self.config, test=True)
            saved = self.status(r)
            self.assertEqual(saved["status"], "updated_verified")
            self.assertEqual(json.loads(saved["original"])["custom_fields"][0]["value"], "")
            self.assertEqual(saved["test_verified"], 1)
            b.process(client, self.store, [r], self.config)
            self.assertEqual(client.writes, 1)

    def test_dry_run_conflicts_and_already_correct_do_not_write(self):
        r = self.rows[0]
        for value, expected in (("", "ready"), ("123", "conflict"), ("009", "already_correct")):
            with self.subTest(value=value):
                self.store.update(r, "pending")
                client = FakeClient(r, value)
                b.process(client, self.store, [r], self.config, dry=True)
                self.assertEqual(self.status(r)["status"], expected)
                self.assertEqual(client.writes, 0)

    def test_uncertain_write_is_reconciled_without_second_write(self):
        r = self.rows[0]
        client = FakeClient(r, uncertain=True)
        b.process(client, self.store, [r], self.config)
        self.assertEqual(self.status(r)["status"], "updated_verified")
        self.assertEqual(client.writes, 1)
        self.assertEqual(client.reads, 2)

    def test_restart_after_write_before_verification(self):
        r = self.rows[0]
        self.store.update(r, "updating", original=document(r))
        client = FakeClient(r, "009")
        b.process(client, self.store, [r], self.config)
        self.assertEqual(self.status(r)["status"], "updated_verified")
        self.assertEqual(client.writes, 0)

    def test_changed_business_fields_halt_and_persist_halt(self):
        r = self.rows[0]
        changes = [lambda d: d.update(total=101), lambda d: d.update(customer_id="99"),
                   lambda d: d["line_items"][0].update(quantity=2),
                   lambda d: d["custom_fields"][1].update(value="CHANGED"),
                   lambda d: d.update(status="draft"), lambda d: d.update(balance=0)]
        for change in changes:
            with self.subTest(change=change):
                with self.store.db:
                    self.store.put("halted", None)
                    self.store.db.execute("UPDATE documents SET status='pending',original=NULL")
                client = FakeClient(r, mutate=change)
                with self.assertRaises(b.VerificationError):
                    b.process(client, self.store, self.rows, self.config)
                self.assertTrue(self.store.get("halted"))
                self.assertEqual(client.writes, 1)
                with self.assertRaises(b.VerificationError):
                    b.process(client, self.store, self.rows, self.config)

    def test_wrong_branch_halts(self):
        r = self.rows[0]
        client = FakeClient(r, mutate=lambda d: d["custom_fields"][0].update(value="123"))
        with self.assertRaises(b.VerificationError):
            b.process(client, self.store, [r], self.config)

    def test_failure_circuit_breaker(self):
        client = Mock()
        client.read.side_effect = b.BackfillError("Read failed")
        with self.assertRaises(b.BackfillError):
            b.process(client, self.store, self.rows, self.config, max_failures=1)
        self.assertEqual(client.read.call_count, 1)
        self.assertEqual(self.status(self.rows[1])["status"], "pending")

    def test_resumed_dry_run_skips_reviewed_rows_without_new_api_calls(self):
        self.store.update(self.rows[0], "ready")
        self.store.update(self.rows[1], "ready_omitted")
        client = Mock()
        b.process(client, self.store, self.rows, self.config, dry=True, resume_dry=True,
                  omitted_schemas={"creditnote": {"api_name": "cf_customer_branch"}})
        client.read.assert_not_called()
        client.write.assert_not_called()
        with self.assertRaisesRegex(b.BackfillError, "read-only"):
            b.process(client, self.store, self.rows, self.config, resume_dry=True)

    def test_dry_run_resume_retries_failed_rows(self):
        self.store.update(self.rows[0], "failed", "Temporary read failure")
        client = FakeClient(self.rows[0])
        b.process(client, self.store, [self.rows[0]], self.config, dry=True, resume_dry=True)
        self.assertEqual(client.reads, 1)
        self.assertEqual(client.writes, 0)
        self.assertEqual(self.status(self.rows[0])["status"], "ready")

    def test_omitted_lookup_review_is_provisional_and_never_writes(self):
        for r in self.rows:
            client = FakeClient(r)
            client.doc["custom_fields"].pop(0)
            client.doc["custom_field_hash"].pop("cf_customer_branch")
            schema = {r["type"]: {"api_name": "cf_customer_branch"}}
            with self.assertRaisesRegex(b.BackfillError, "read-only"):
                b.process(client, self.store, [r], self.config, omitted_schemas=schema)
            b.process(client, self.store, [r], self.config, dry=True, omitted_schemas=schema)
            self.assertEqual(self.status(r)["status"], "ready_omitted")
            self.assertIsNone(self.status(r)["original"])
            self.assertEqual(client.writes, 0)
        self.assertEqual(self.store.report(self.folder)["pending"], 2)

    def test_nonempty_mirrors_block_omitted_lookup_classification(self):
        r = self.rows[0]
        for mirror in ("cf_customer_branch", "cf_customer_branch_formatted"):
            for use_hash in (True, False):
                with self.subTest(mirror=mirror, use_hash=use_hash):
                    client = FakeClient(r)
                    client.doc["custom_fields"].pop(0)
                    client.doc["custom_field_hash"].pop("cf_customer_branch")
                    (client.doc["custom_field_hash"] if use_hash else client.doc)[mirror] = "123"
                    b.process(client, self.store, [r], self.config, dry=True,
                              omitted_schemas={r["type"]: {"api_name": "cf_customer_branch"}})
                    self.assertEqual(self.status(r)["status"], "failed")
                    self.assertEqual(client.writes, 0)

    def test_present_lookup_conflict_is_not_bypassed_by_omission_review(self):
        r = self.rows[0]
        client = FakeClient(r, "123")
        b.process(client, self.store, [r], self.config, dry=True,
                  omitted_schemas={r["type"]: {"api_name": "cf_customer_branch"}})
        self.assertEqual(self.status(r)["status"], "conflict")
        self.assertEqual(client.writes, 0)

    def test_restart_before_write_reads_then_retries_with_original_preserved(self):
        r = self.rows[0]
        original = document(r)
        self.store.update(r, "updating", original=original)
        client = FakeClient(r)
        b.process(client, self.store, [r], self.config)
        self.assertEqual(client.writes, 1)
        self.assertEqual(client.reads, 2)
        self.assertEqual(json.loads(self.status(r)["original"]), original)

    def test_missing_target_or_unsupported_lookup_after_write_halts(self):
        r = self.rows[0]
        for change in (lambda d: d["custom_fields"].pop(0),
                       lambda d: d["custom_fields"][0].update(value={"id": "009"})):
            with self.subTest(change=change):
                with self.store.db:
                    self.store.put("halted", None)
                    self.store.db.execute("UPDATE documents SET status='pending',original=NULL")
                with self.assertRaises(b.VerificationError):
                    b.process(FakeClient(r, mutate=change), self.store, [r], self.config)
                self.assertTrue(self.store.get("halted"))

    def test_concurrent_output_lock_rejected(self):
        with b.run_lock(self.folder):
            with self.assertRaises(b.BackfillError):
                with b.run_lock(self.folder):
                    self.fail("Acquired duplicate lock")

    def test_identity_change_rejected(self):
        for identity in ({**self.identity, "sha256": "changed"}, {**self.identity, "ZOHO_ORG_ID": "2"}):
            with self.assertRaises(b.BackfillError):
                b.Store(self.folder / "progress.sqlite3", self.rows, identity)

    def test_report_reconciles_including_uncertain_and_ready(self):
        self.store.update(self.rows[0], "updating", original=document(self.rows[0]))
        self.store.update(self.rows[1], "ready")
        summary = self.store.report(self.folder)
        self.assertEqual(summary["total_mapped"], 2)
        self.assertEqual(summary["pending"], 2)
        self.assertEqual(sum(v for k, v in summary.items() if k != "total_mapped"), 2)


def response(status=200, body=None, headers=None):
    result = Mock(status_code=status, headers=headers or {})
    result.json.return_value = {"code": 0} if body is None else body
    return result


class TransportTests(unittest.TestCase):
    def test_request_rate_cap_counts_refresh_and_network_latency(self):
        now = [0.0]
        started = []
        def sleep(seconds):
            now[0] += seconds
        def network(*args, **kwargs):
            started.append(now[0])
            now[0] += 0.4
            return response()
        def oauth(*args, **kwargs):
            started.append(now[0])
            now[0] += 0.2
            return response(body={"access_token": "SECRET"})
        self.session.post.side_effect = oauth
        self.session.request.side_effect = network
        client = b.Client(self.client.config, delay=0, session=self.session, sleep=sleep,
                          requests_per_minute=85, clock=lambda: now[0])
        for _ in range(3):
            client.request("GET", "/invoices/1")
        self.assertEqual(len(started), 4)
        for before, after in zip(started, started[1:]):
            self.assertGreaterEqual(after - before + 1e-9, 60 / 85)
        self.assertAlmostEqual(started[-1], 3 * 60 / 85)

    def test_slow_network_does_not_add_an_extra_request_interval(self):
        now = [0.0]
        slept = []
        client = b.Client({}, delay=0, sleep=slept.append,
                          requests_per_minute=85, clock=lambda: now[0])
        client.pace()
        now[0] = 2.0
        client.pace()
        self.assertEqual(slept, [])

    def setUp(self):
        self.session = Mock()
        self.session.post.return_value = response(body={"access_token": "SECRET"})
        self.sleep = Mock()
        self.client = b.Client({"ZOHO_ACCOUNTS": "https://accounts.zoho.com",
            "ZOHO_API": "https://www.zohoapis.com/books/v3", "ZOHO_ORG_ID": "1",
            "ZOHO_CLIENT_ID": "id", "ZOHO_CLIENT_SECRET": "secret", "ZOHO_REFRESH_TOKEN": "refresh"},
            delay=0, retries=2, session=self.session, sleep=self.sleep)

    def test_expired_token_renews(self):
        self.session.request.side_effect = [response(401), response()]
        self.client.request("GET", "/invoices/1")
        self.assertEqual(self.session.post.call_count, 2)

    def test_exhausted_rate_limit_retains_api_evidence(self):
        body = {"code": 45, "message": "API call limit exceeded"}
        self.session.request.side_effect = [response(429, body=body)] * 3
        with self.assertRaises(b.APIError) as caught:
            self.client.request("GET", "/invoices/1")
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(caught.exception.body, body)

    def test_write_401_preserves_scope_error_without_resend(self):
        body = {"code": 57, "message": "Insufficient scope"}
        self.session.request.return_value = response(401, body=body)
        with self.assertRaises(b.APIError) as caught:
            self.client.request("POST", "/creditnotes/1/customfields", {})
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(caught.exception.body, body)
        self.assertNotIn("expired", str(caught.exception))
        self.assertEqual(self.session.request.call_count, 1)

    def test_rate_limit_and_network_read_retry(self):
        self.session.request.side_effect = [response(429, headers={"Retry-After": "2"}),
                                            requests.Timeout(), response()]
        self.client.request("GET", "/invoices/1")
        self.assertEqual(self.session.request.call_count, 3)
        self.sleep.assert_any_call(2)

    def test_uncertain_writes_never_retried_at_transport_layer(self):
        for problem in (requests.Timeout(), response(500), response(429), response(401)):
            with self.subTest(problem=problem):
                self.session.request.reset_mock()
                self.session.request.side_effect = [problem]
                with self.assertRaises(b.BackfillError):
                    self.client.request("PUT", "/invoice/1/customfields", {})
                self.assertEqual(self.session.request.call_count, 1)

    def test_retry_exhaustion(self):
        self.session.request.side_effect = requests.ConnectionError()
        with self.assertRaises(b.BackfillError):
            self.client.request("GET", "/invoices/1")
        self.assertEqual(self.session.request.call_count, 3)

    def test_payloads_only_target_branch(self):
        self.client.request = Mock()
        for t in b.EXPECTED:
            r = row(t)
            self.client.write(r, "7", document(r))
            method, path, payload = self.client.request.call_args.args
            if t == "invoice":
                self.assertEqual((method, path), ("PUT", "/invoice/001/customfields"))
                self.assertEqual(payload, {"custom_fields": [{"customfield_id": "7", "value": "009"}]})
            else:
                self.assertEqual((method, path), ("POST", "/creditnotes/001/customfields"))
                self.assertEqual(payload, {"custom_fields": [{"index": 2, "value": "009"}]})

    def test_read_rejects_wrong_document_or_number(self):
        r = row()
        for doc in ({**document(r), "invoice_id": "999"}, {**document(r), "invoice_number": "OTHER"}):
            self.client.request = Mock(return_value={"invoice": doc})
            with self.assertRaises(b.BackfillError):
                self.client.read(r)


class CLIGateTests(unittest.TestCase):
    def test_difference_paths_preserve_types_and_missing_values(self):
        before = {"a/b": [1, None], "flag": True, "deleted": None}
        after = {"a/b": [1, "changed"], "flag": 1, "added": None}
        differences = b.document_differences(before, after)
        self.assertEqual({d["path"] for d in differences}, {"/a~1b/1", "/flag", "/deleted", "/added"})
        deleted = next(d for d in differences if d["path"] == "/deleted")
        self.assertTrue(deleted["before_present"])
        self.assertFalse(deleted["after_present"])

    def test_omission_schema_requires_exact_field_identity_and_permissions(self):
        for module in b.EXPECTED:
            field = {"field_id": "7", "api_name": "cf_customer_branch", "data_type": "lookup",
                     "entity": module, "related_entity": "cm_customer_branch", "permission": "write",
                     "visibility": "all", "is_active": True, "is_read_only": False, "is_custom_field": True}
            self.assertEqual(b.validate_lookup_schema({"fields": [field]}, "7", module)["field_id"], "7")
            for key, value in (("permission", "read"), ("visibility", "hidden"), ("is_active", False),
                               ("is_read_only", True), ("entity", "other"), ("data_type", "dropdown"),
                               ("related_entity", "cm_other"), ("is_custom_field", False)):
                with self.subTest(module=module, key=key), self.assertRaises(b.BackfillError):
                    b.validate_lookup_schema({"fields": [{**field, key: value}]}, "7", module)

    def test_field_metadata_reads_both_module_schemas_without_document_writes(self):
        for module in b.EXPECTED:
            with self.subTest(module=module), patch.object(b, "settings", return_value={
                    "CF_" + module.upper() + "_ID": "7"}) as settings, \
                 patch.object(b, "Client") as client, patch("builtins.print") as printed:
                client.return_value.request.return_value = {"fields": [{"field_id": "7",
                    "api_name": "cf_customer_branch", "data_type": "lookup", "is_active": True,
                    "default_value": "PRIVATE", "values": [{"value": "PRIVATE"}]}]}
                self.assertEqual(b.main(["--field-metadata", "--type", module]), 0)
                settings.assert_called_once_with([module])
                client.return_value.request.assert_called_once_with("GET", "/settings/fields",
                    query={"entity": module, "field_ids": "7"})
                self.assertNotIn("PRIVATE", printed.call_args.args[0])
                self.assertIn("cf_customer_branch", printed.call_args.args[0])

    def test_field_metadata_rejects_missing_duplicate_and_malformed_fields(self):
        for body in ({}, {"fields": []}, {"fields": [None]},
                     {"fields": [{"field_id": "8"}]},
                     {"fields": [{"field_id": "7"}, {"field_id": "7"}]}):
            with self.subTest(body=body), self.assertRaises(b.BackfillError):
                b.lookup_metadata(body, "7")

    def test_diagnostic_omits_customer_values_and_distinguishes_absent_fields(self):
        doc = document(row())
        doc["customer_name"] = "PRIVATE CUSTOMER"
        doc["custom_fields"][0]["value"] = "PRIVATE BRANCH"
        doc["cf_customer_branch"] = "PRIVATE BRANCH"
        result = b.field_diagnostic(doc)
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertTrue(result["custom_fields_present"])
        self.assertEqual(result["custom_fields_shape"]["count"], 2)
        del doc["custom_fields"]
        self.assertFalse(b.field_diagnostic(doc)["custom_fields_present"])

    def test_diagnose_only_reads_requested_document(self):
        with patch.object(b, "settings", return_value={}) as settings, \
             patch.object(b, "Client") as client, patch("builtins.print") as printed:
            client.return_value.request.return_value = {"invoice": document(row())}
            self.assertEqual(b.main(["--diagnose", "001", "--type", "invoice"]), 0)
            settings.assert_called_once_with(["invoice"], fields=False)
            client.return_value.request.assert_called_once_with("GET", "/invoices/001")
            self.assertIn("custom_fields_present", printed.call_args.args[0])

    def test_full_run_requires_both_live_tests_and_manual_approvals(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = [row(), row("creditnote", "002")]
            config = {"ZOHO_ORG_ID": "1", "ZOHO_ACCOUNTS": "https://accounts.zoho.com",
                      "ZOHO_API": "https://www.zohoapis.com/books/v3",
                      "CF_INVOICE_ID": "7", "CF_CREDITNOTE_ID": "7"}
            with patch.object(b, "load_map", return_value=(rows, "abc")), \
                 patch.object(b, "settings", return_value=config), \
                 patch.object(b.logging, "basicConfig"), \
                 patch.object(b, "Client") as client, patch("builtins.print"):
                with self.assertRaisesRegex(b.BackfillError, "Full run requires"):
                    b.main(["--run", "--output", directory])
                client.assert_not_called()
                with self.assertRaisesRegex(b.BackfillError, "No changed"):
                    b.main(["--approve-test", "invoice", "--review-note", "UI checked", "--output", directory])
                db = b.sqlite3.connect(Path(directory) / "progress.sqlite3")
                with db:
                    db.execute("UPDATE documents SET status='updated_verified',test_verified=1")
                db.close()
                for t in b.EXPECTED:
                    b.main(["--approve-test", t, "--review-note", "UI checked", "--output", directory])
                b.main(["--run", "--output", directory])
                client.assert_called_once()

    def test_discovery_both_modules_needs_no_custom_field_settings(self):
        for t in b.EXPECTED:
            with self.subTest(type=t), patch.object(b, "settings", return_value={}) as settings, \
                 patch.object(b, "Client") as client, patch("builtins.print"):
                client.return_value.request.return_value = {t: document(row(t))}
                self.assertEqual(b.main(["--discover", "001", "--type", t]), 0)
                settings.assert_called_once_with([t], fields=False)
                client.return_value.request.assert_called_once_with("GET", f"/{t}s/001")


if __name__ == "__main__":
    unittest.main()
