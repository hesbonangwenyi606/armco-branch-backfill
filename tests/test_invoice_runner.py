from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from backfill_customer_branch import BackfillError, Store
from run_invoice_backfill import update_one, reconcile_read_failure, reconcile_interrupted
from test_backfill import FakeOmittedClient, document, invoice_schema, row


class InvoiceRunnerTests(unittest.TestCase):
    def setUp(self):
        self.row = row()
        self.schema = invoice_schema()

    def client(self, mutate=None):
        client = FakeOmittedClient(self.row, mutate)
        client.payloads = []
        def request(method, path, payload=None):
            if method == "GET":
                return {"custom_fields": [{**document(self.row)["custom_fields"][0], "data_type": "lookup"}]}
            client.payloads.append((method, path, deepcopy(payload)))
            client.write(self.row, "7", client.doc)
            return {"code": 0}
        client.request = request
        return client

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
