from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from backfill_customer_branch import BackfillError
from test_backfill import FakeClient, document, invoice_schema, row
from test_creditnote_branch import (DOCUMENT_ID, FIELD_ID, execute, prepare, reviewed_field,
                                   index_failure_baseline)


class CreditNoteTestTests(unittest.TestCase):
    def setUp(self):
        self.row = {**row("creditnote", DOCUMENT_ID), "number": "CN027182"}
        self.schema = {**invoice_schema(), "field_id": FIELD_ID, "entity": "creditnote"}
        self.doc = document(self.row)
        self.doc["custom_fields"][0].update(customfield_id=FIELD_ID, field_id=FIELD_ID,
                                            index=1, data_type="lookup")
        self.review = {"code": 0, "creditnote": deepcopy(self.doc)}
        self.before = deepcopy(self.doc)
        self.before["custom_fields"].pop(0)
        self.before["custom_field_hash"].pop("cf_customer_branch")

    def client(self, mutate=None, uncertain=False):
        client = FakeClient(self.row)
        client.doc = deepcopy(self.before)
        def write(row, field, shaped):
            client.writes += 1
            if field != FIELD_ID or shaped["custom_fields"][-1]["index"] != 1:
                raise AssertionError("Wrong target field")
            saved = deepcopy(self.doc["custom_fields"][0])
            saved["value"] = row["branch_record_id"]
            client.doc["custom_fields"].append(saved)
            client.doc["custom_field_hash"]["cf_customer_branch"] = row["branch_record_id"]
            if mutate:
                mutate(client.doc)
            if uncertain:
                raise BackfillError("Uncertain write")
            return {"code": 0}
        client.write = write
        return client

    def test_omitted_lookup_uses_reviewed_document_index(self):
        shaped = prepare(self.before, self.review, self.row, self.schema)
        self.assertEqual(shaped["custom_fields"][-1]["index"], 1)
        self.assertEqual(len(self.before["custom_fields"]), 1)

    def test_wrong_review_identity_index_or_populated_value_blocks(self):
        for key, value in (("index", 2), ("value", "999"), ("customfield_id", "other")):
            review = deepcopy(self.review)
            review["creditnote"]["custom_fields"][0][key] = value
            with self.subTest(key=key), self.assertRaises(BackfillError):
                reviewed_field(review, self.row)
        with self.assertRaises(BackfillError):
            reviewed_field(self.review, {**self.row, "zoho_id": "other"})

    def test_index_collision_and_populated_mirror_block(self):
        for change in (lambda d: d["custom_fields"][0].update(index=1),
                       lambda d: d["custom_field_hash"].update(cf_customer_branch_unformatted="999")):
            before = deepcopy(self.before)
            change(before)
            with self.assertRaises(BackfillError):
                prepare(before, self.review, self.row, self.schema)

    def test_preview_never_writes(self):
        client = self.client()
        with tempfile.TemporaryDirectory() as temp:
            execute(client, self.row, self.schema, self.review, Path(temp))
            self.assertEqual(client.writes, 0)
            self.assertFalse((Path(temp) / "attempt.json").exists())

    def test_auth_retry_blocks_changes_since_previous_failure(self):
        baseline = deepcopy(self.before)
        baseline["total"] = 999
        client = self.client()
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(BackfillError):
            execute(client, self.row, self.schema, self.review, Path(temp), True, baseline)
        self.assertEqual(client.writes, 0)

    def test_field_id_put_sends_only_branch_and_cannot_resend(self):
        client = self.client()
        shaped = prepare(self.before, self.review, self.row, self.schema)
        client.request = Mock(side_effect=lambda *args: client.write(self.row, FIELD_ID, shaped))
        with tempfile.TemporaryDirectory() as temp:
            result = execute(client, self.row, self.schema, self.review, Path(temp),
                             True, self.before, True)
            self.assertTrue(result["test_verified"])
            client.request.assert_called_once_with("PUT", "/creditnotes/" + DOCUMENT_ID,
                {"custom_fields": [{"customfield_id": FIELD_ID, "value": self.row["branch_record_id"]}]})
            with self.assertRaises(BackfillError):
                execute(client, self.row, self.schema, self.review, Path(temp), True, self.before, True)
            self.assertEqual(client.request.call_count, 1)

    def test_index_failure_baseline_rejects_other_errors_or_business_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            evidence = {
                "before": self.before, "after": self.before, "mapping": self.row,
                "write_outcome": {"http_status": 400, "response": {"code": 120121}},
                "summary": {"test_verified": False, "remaining_business_changed_paths": []},
                "attempt": {"method": "POST", "path": "/creditnotes/" + DOCUMENT_ID + "/customfields",
                            "payload": {"custom_fields": [{"index": 1, "value": self.row["branch_record_id"]}]}}}
            for name, value in evidence.items():
                (folder / (name + ".json")).write_text(json.dumps(value))
            self.assertEqual(index_failure_baseline(folder, self.row, self.schema), self.before)
            (folder / "write_outcome.json").write_text(json.dumps({"http_status": 400, "response": {"code": 12069}}))
            with self.assertRaises(BackfillError):
                index_failure_baseline(folder, self.row, self.schema)

    def test_success_reconciles_uncertain_write_and_blocks_resend(self):
        client = self.client(uncertain=True)
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            result = execute(client, self.row, self.schema, self.review, folder, True)
            self.assertTrue(result["test_verified"])
            self.assertTrue((folder / "before.json").exists())
            with self.assertRaises(BackfillError):
                execute(client, self.row, self.schema, self.review, folder, True)
            self.assertEqual(client.writes, 1)

    def test_business_or_other_custom_field_changes_fail_verification(self):
        for mutate in (lambda d: d.update(total=999),
                       lambda d: d["line_items"][0].update(quantity=5),
                       lambda d: d["custom_fields"][0].update(value="changed")):
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as temp:
                with self.assertRaises(BackfillError):
                    execute(self.client(mutate), self.row, self.schema, self.review, Path(temp), True)
                self.assertFalse(json.loads((Path(temp) / "summary.json").read_text())["test_verified"])


if __name__ == "__main__":
    unittest.main()
