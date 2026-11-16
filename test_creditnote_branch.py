"""Guarded one-document API test for the reviewed blank branch on CN027182.

The bulk database is read-only. A durable attempt marker prevents automatic resend.
"""
import argparse
import contextlib
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

from backfill_customer_branch import (BackfillError, Client, branch_value,
    document_differences, dumps, find_field, load_map, protected, run_lock,
    settings, validate_lookup_schema)

DOCUMENT_ID = "1462401000008001966"
FIELD_ID = "1462401000020819915"
NUMBER = "CN027182"


def reviewed_field(body, row):
    doc = body.get("creditnote", {})
    if (body.get("code") != 0 or doc.get("creditnote_id") != DOCUMENT_ID or
            doc.get("creditnote_number") != NUMBER or row["zoho_id"] != DOCUMENT_ID or
            row["type"] != "creditnote" or row["number"] != NUMBER):
        raise BackfillError("Review must be the captured edit response for CN027182")
    field = find_field(doc, FIELD_ID)
    if (field.get("api_name") != "cf_customer_branch" or field.get("data_type") != "lookup" or
            type(field.get("index")) is not int or field["index"] != 1 or
            branch_value(doc, FIELD_ID) != ""):
        raise BackfillError("Reviewed document must show the blank branch at document index 1")
    return field


def projection(doc, schema):
    result = protected(doc, FIELD_ID, schema)
    # Preserve every business field; these are target-field mirrors and audit metadata.
    result.pop("last_modified_by_id", None)
    result.pop("cf_customer_branch_unformatted", None)
    if isinstance(result.get("custom_field_hash"), dict):
        result["custom_field_hash"].pop("cf_customer_branch_unformatted", None)
    return result


def prepare(before, reviewed, row, schema):
    field = reviewed_field(reviewed, row)
    if (before.get("creditnote_id") != DOCUMENT_ID or before.get("creditnote_number") != NUMBER or
            not before.get("customer_id") or
            before["customer_id"] != reviewed["creditnote"].get("customer_id")):
        raise BackfillError("Fresh document identity/customer differs from reviewed document")
    if branch_value(before, FIELD_ID, schema) != "":
        raise BackfillError("Customer Branch is already populated; no write permitted")
    for container in (before, before.get("custom_field_hash", {})):
        if container.get("cf_customer_branch_unformatted") not in (None, ""):
            raise BackfillError("Unformatted branch mirror is populated")
    for item in before["custom_fields"]:
        if str(item.get("index")) == str(field["index"]) and str(item.get("customfield_id")) != FIELD_ID:
            raise BackfillError("Document index 1 belongs to another custom field")
    shaped = deepcopy(before)
    if not any(str(item.get("customfield_id")) == FIELD_ID for item in shaped["custom_fields"]):
        shaped["custom_fields"].append(deepcopy(field))
    actual = find_field(shaped, FIELD_ID)
    if type(actual.get("index")) is not int or actual["index"] != 1:
        raise BackfillError("Fresh document branch index differs from reviewed index")
    return shaped


def save(folder, name, value):
    path = folder / (name + ".json")
    # Exclusive creation protects original evidence and the pre-write marker.
    with path.open("x", encoding="utf-8") as file:
        file.write(dumps(value) + "\n")
        file.flush()
        import os
        os.fsync(file.fileno())


def auth_retry_baseline(folder, row, schema):
    """Authorize only the investigated authentication failure, retaining all evidence."""
    outcome = json.loads((folder / "write_outcome.json").read_text(encoding="utf-8"))
    summary = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    before = json.loads((folder / "before.json").read_text(encoding="utf-8"))
    after = json.loads((folder / "after.json").read_text(encoding="utf-8"))
    mapping = json.loads((folder / "mapping.json").read_text(encoding="utf-8"))
    attempt = json.loads((folder / "attempt.json").read_text(encoding="utf-8"))
    expected = {"method": "POST", "path": "/creditnotes/" + DOCUMENT_ID + "/customfields",
                "payload": {"custom_fields": [{"index": 1, "value": row["branch_record_id"]}]}}
    if (mapping != row or attempt != expected or
            outcome.get("error") != "Write authentication expired; reconcile before retry" or
            summary.get("test_verified") is not False or
            summary.get("remaining_business_changed_paths") != [] or
            projection(before, schema) != projection(after, schema) or
            branch_value(after, FIELD_ID, schema) != ""):
        raise BackfillError("Previous evidence does not establish the investigated authentication failure")
    return before


def index_failure_baseline(folder, row, schema):
    """Require the recorded index rejection and unchanged readback before a new format test."""
    outcome = json.loads((folder / "write_outcome.json").read_text(encoding="utf-8"))
    summary = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    before = json.loads((folder / "before.json").read_text(encoding="utf-8"))
    after = json.loads((folder / "after.json").read_text(encoding="utf-8"))
    mapping = json.loads((folder / "mapping.json").read_text(encoding="utf-8"))
    attempt = json.loads((folder / "attempt.json").read_text(encoding="utf-8"))
    if (mapping != row or outcome.get("http_status") != 400 or
            outcome.get("response", {}).get("code") != 120121 or
            summary.get("test_verified") is not False or
            summary.get("remaining_business_changed_paths") != [] or
            attempt != {"method": "POST", "path": "/creditnotes/" + DOCUMENT_ID + "/customfields",
                        "payload": {"custom_fields": [{"index": 1, "value": row["branch_record_id"]}]}} or
            projection(before, schema) != projection(after, schema) or
            branch_value(after, FIELD_ID, schema) != ""):
        raise BackfillError("Saved evidence does not establish the investigated index rejection")
    return before


def execute(client, row, schema, reviewed, folder, apply=False, retry_baseline=None, field_id_test=False):
    if (folder / "attempt.json").exists():
        raise BackfillError("A write was already attempted; audit saved evidence before any further test")
    before = client.read(row)
    shaped = prepare(before, reviewed, row, schema)
    if retry_baseline is not None and projection(before, schema) != projection(retry_baseline, schema):
        raise BackfillError("Document changed since previous failure; test blocked")
    method = "PUT" if field_id_test else "POST"
    path = "/creditnotes/" + DOCUMENT_ID + ("" if field_id_test else "/customfields")
    payload = {"custom_fields": [{("customfield_id" if field_id_test else "index"):
                                  (FIELD_ID if field_id_test else 1), "value": row["branch_record_id"]}]}
    summary = {"creditnote_number": NUMBER, "applied": False, "halt_retained": True,
               "evidence_folder": str(folder), "payload": payload}
    if not apply:
        print(dumps(summary))
        return summary
    if (folder / "before.json").exists():
        raise BackfillError("Existing baseline requires investigation; no write sent")
    save(folder, "before", before)
    save(folder, "schema", schema)
    save(folder, "review", reviewed)
    save(folder, "mapping", row)
    # Detect a concurrent edit before committing the single request.
    fresh = client.read(row)
    if (projection(before, schema) != projection(fresh, schema) or
            branch_value(fresh, FIELD_ID, schema) != ""):
        raise BackfillError("Document changed during preflight; no write sent")
    save(folder, "attempt", {"method": method, "path": path, "payload": payload})
    summary["applied"] = True
    try:
        response = (client.request(method, path, payload) if field_id_test
                    else client.write(row, FIELD_ID, shaped))
        outcome = {"response": response}
    except BackfillError as error:
        outcome = {"error": str(error), "http_status": getattr(error, "status", None),
                   "response": getattr(error, "body", None)}
    save(folder, "write_outcome", outcome)
    after = client.read(row)
    save(folder, "after", after)
    # Post-write absence is never interpreted as a successfully saved blank/value.
    changes = document_differences(projection(before, schema), projection(after, schema))
    summary["remaining_business_changed_paths"] = [item["path"] for item in changes]
    try:
        summary["expected_branch_matches"] = branch_value(after, FIELD_ID) == row["branch_record_id"]
    except BackfillError as error:
        summary["expected_branch_matches"] = False
        summary["branch_read_error"] = str(error)
    summary["test_verified"] = not changes and summary["expected_branch_matches"]
    if "error" in outcome:
        summary["request_error"] = outcome["error"]
    save(folder, "summary", summary)
    print(dumps(summary))
    if not summary["test_verified"]:
        raise BackfillError("Test not verified. Halt retained; do not repeat the write")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--retry-after-auth", action="store_true",
                        help="One separately recorded retry after the investigated OAuth failure")
    modes.add_argument("--test-field-id", action="store_true",
                       help="One separately recorded branch-only PUT using the browser-observed field ID")
    args = parser.parse_args(argv)
    root = Path("output/corrected_run")
    folder = root / "creditnote_CN027182_test"
    review_path = root / "creditnote_CN027182_review.json"
    raw = review_path.read_bytes()
    expected_review_hash = "deac15094c24212ee8729e9ec51042f87fe6775a46776a5d25962e0f974564b0"
    if hashlib.sha256(raw).hexdigest() != expected_review_hash:
        raise BackfillError("Captured review evidence changed")
    reviewed = json.loads(raw)
    rows, fingerprint = load_map("data/Armco_Branch_Backfill_API_Map_corrected.csv")
    row = next(item for item in rows if item["type"] == "creditnote" and item["zoho_id"] == DOCUMENT_ID)
    reviewed_field(reviewed, row)
    cfg = settings(["creditnote"])
    if cfg["CF_CREDITNOTE_ID"] != FIELD_ID:
        raise BackfillError("Configured credit note field differs from reviewed field")
    with run_lock(root):
        with contextlib.closing(sqlite3.connect((root / "progress.sqlite3").resolve().as_uri() + "?mode=ro", uri=True)) as db:
            identity = json.loads(db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()[0])
            saved = db.execute("SELECT mapping FROM documents WHERE type='creditnote' AND zoho_id=?", (DOCUMENT_ID,)).fetchone()
        for key in ("ZOHO_ORG_ID", "ZOHO_ACCOUNTS", "ZOHO_API", "CF_CREDITNOTE_ID"):
            if cfg[key] != identity.get(key):
                raise BackfillError("Settings differ from the approved bulk run")
        if fingerprint not in identity.values() or not saved or json.loads(saved[0]) != row:
            raise BackfillError("Map differs from the approved bulk run")
        client = Client(cfg)
        schema = validate_lookup_schema(client.request("GET", "/settings/fields",
            query={"entity": "creditnote"}), FIELD_ID, "creditnote")
        baseline = None
        if args.retry_after_auth:
            baseline = auth_retry_baseline(folder, row, schema)
            folder = root / "creditnote_CN027182_auth_retry"
        elif args.test_field_id:
            baseline = index_failure_baseline(root / "creditnote_CN027182_auth_retry", row, schema)
            folder = root / "creditnote_CN027182_field_id_test"
        folder.mkdir(exist_ok=True)
        execute(client, row, schema, reviewed, folder, args.apply, baseline, args.test_field_id)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (BackfillError, OSError, sqlite3.Error, ValueError, KeyError, StopIteration) as error:
        print("Credit-note test stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
