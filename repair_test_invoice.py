"""Restore the zero-adjustment description on the single investigated test invoice."""
import argparse
import contextlib
import json
from pathlib import Path
import sqlite3
import sys
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from backfill_customer_branch import (APIError, BackfillError, Client, branch_value,
    document_differences, dumps, protected, run_lock, settings, validate_lookup_schema)


def projection(doc, field_id, schema):
    value = protected(doc, field_id, schema)
    value.pop("last_modified_by_id", None)
    hashed = value.get("custom_field_hash")
    if isinstance(hashed, dict):
        hashed.pop(schema["api_name"] + "_unformatted", None)
    link = value.get("invoice_url")
    if isinstance(link, str):
        parts = urlsplit(link)
        if parts.scheme == "https" and parts.netloc == "zohosecurepay.com":
            pairs = [(key, "<generated-token>" if key == "CInvoiceID" else item)
                     for key, item in parse_qsl(parts.query, keep_blank_values=True)]
            value["invoice_url"] = urlunsplit((parts.scheme, parts.netloc, parts.path,
                                               urlencode(pairs), parts.fragment))
    return value


def check_plan(original, current, row, schema):
    field_id = schema["field_id"]
    if row["type"] != "invoice" or row["zoho_id"] != "1462401000001974003" or row["number"] != "152522":
        raise BackfillError("Repair is restricted to investigated invoice 152522")
    if original.get("adjustment") != 0 or current.get("adjustment") != 0 or original.get("adjustment_description") != "":
        raise BackfillError("Unexpected original adjustment; repair blocked")
    if branch_value(current, field_id) != row["branch_record_id"]:
        raise BackfillError("Expected Customer Branch is not saved; repair blocked")
    differences = document_differences(projection(original, field_id, schema), projection(current, field_id, schema))
    if not differences:
        return None
    if ([d["path"] for d in differences] != ["/adjustment_description"] or
            current.get("adjustment_description") != "Adjustment"):
        raise BackfillError("Other business data changed; repair blocked")
    return {"adjustment_description": ""}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("output/corrected_run"))
    parser.add_argument("--apply", action="store_true", help="Send the one-field repair after precondition checks")
    args = parser.parse_args(argv)
    cfg = settings(["invoice"])
    database = args.output / "progress.sqlite3"
    if not database.is_file():
        raise BackfillError("Original run database missing")
    with run_lock(args.output):
        with contextlib.closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            saved = db.execute("SELECT mapping,original FROM documents WHERE type='invoice' AND zoho_id='1462401000001974003'").fetchone()
            identity = json.loads(db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()[0])
            confirmation = json.loads(db.execute("SELECT value FROM meta WHERE key='invoice_ui_empty_confirmation'").fetchone()[0])
        if not saved or not saved[1] or confirmation.get("document_id") != "1462401000001974003":
            raise BackfillError("Original snapshot or test confirmation missing")
        for key in ("ZOHO_ORG_ID", "ZOHO_ACCOUNTS", "ZOHO_API", "CF_INVOICE_ID"):
            if cfg[key] != identity[key]:
                raise BackfillError("Settings differ from original test run")
        row, original, schema = json.loads(saved[0]), json.loads(saved[1]), confirmation["schema"]
        validate_lookup_schema({"fields": [schema]}, cfg["CF_INVOICE_ID"], "invoice")
        client = Client(cfg)
        current = client.read(row)
        payload = check_plan(original, current, row, schema)
        destination = args.output / ("description_repair_" + str(time.time_ns()))
        destination.mkdir()
        for name, value in (("original", original), ("before", current), ("payload", payload)):
            (destination / (name + ".json")).write_text(dumps(value) + "\n", encoding="utf-8")
        summary = {"invoice_number": row["number"], "payload": payload,
                   "applied": False, "halt_retained": True, "evidence_folder": str(destination)}
        if args.apply and payload:
            summary["applied"] = True
            try:
                response = client.request("PUT", "/invoices/" + row["zoho_id"], payload)
                result = {"response": response}
            except BackfillError as error:
                result = {"error": str(error), "http_status": getattr(error, "status", None),
                          "response": getattr(error, "body", None)}
            (destination / "write_outcome.json").write_text(dumps(result) + "\n", encoding="utf-8")
            after = client.read(row)
            (destination / "after.json").write_text(dumps(after) + "\n", encoding="utf-8")
            remaining = document_differences(projection(original, schema["field_id"], schema),
                                             projection(after, schema["field_id"], schema))
            summary["remaining_business_changed_paths"] = [d["path"] for d in remaining]
            summary["expected_branch_matches"] = branch_value(after, schema["field_id"]) == row["branch_record_id"]
            summary["repair_verified"] = not remaining and summary["expected_branch_matches"]
            if "error" in result:
                summary["request_error"] = result["error"]
        elif payload is None:
            summary["repair_verified"] = True
            summary["note"] = "Description already restored; no write sent"
        (destination / "summary.json").write_text(dumps(summary) + "\n", encoding="utf-8")
        print(dumps(summary))
        if args.apply and not summary.get("repair_verified"):
            raise BackfillError("Repair not verified; inspect saved response. Halt remains; do not repeat blindly")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (BackfillError, OSError, sqlite3.Error) as error:
        print("Repair stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
