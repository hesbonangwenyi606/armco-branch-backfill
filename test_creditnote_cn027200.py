"""One guarded CN027200 test; never automatically repeat a saved attempt."""
import json
import os
from pathlib import Path
import sys

from backfill_customer_branch import (APIError, BackfillError, Client, branch_value, document_differences,
    dumps, load_map, protected, settings, validate_lookup_schema)

CN = "1462401000008003279"
INV = "1462401000001984300"
FIELD = "1462401000020819915"
FOLDER = Path("output/corrected_run/creditnote_CN027200_guarded_test")
ORIGINAL = FOLDER


def save(name, data):
    with (FOLDER / (name + ".json")).open("x", encoding="utf-8") as f:
        f.write(dumps(data) + "\n")
        f.flush()
        os.fsync(f.fileno())


def field_value(body):
    fields = body.get("custom_fields")
    if not isinstance(fields, list):
        raise BackfillError("Missing custom-fields response")
    found = [f for f in fields if str(f.get("customfield_id")) == FIELD]
    if len(found) != 1 or "value" not in found[0]:
        raise BackfillError("Branch field is absent, ambiguous, or has no value")
    return str(found[0]["value"] or "")


def main():
    global FOLDER
    if sys.argv[1:] == ["--diagnostic-retry"]:
        FOLDER = Path("output/corrected_run/creditnote_CN027200_diagnostic_retry")
        error = json.loads((ORIGINAL / "write_error.json").read_text(encoding="utf-8"))
        if error != {"error": "API rejected request (HTTP 400)"}:
            raise BackfillError("Original evidence does not match the specific HTTP 400 rejection")
    elif sys.argv[1:]:
        raise BackfillError("Unknown argument")
    if FOLDER.exists():
        raise BackfillError("Test evidence already exists; inspect it, do not resend")
    rows, fingerprint = load_map("data/Armco_Branch_Backfill_API_Map_corrected.csv")
    cn = next(r for r in rows if r["type"] == "creditnote" and r["zoho_id"] == CN)
    inv = next(r for r in rows if r["type"] == "invoice" and r["zoho_id"] == INV)
    if cn["number"] != "CN027200" or inv["number"] != "152637":
        raise BackfillError("Approved mapping does not match the reviewed documents")
    cfg = settings(["creditnote", "invoice"])
    if cfg["ZOHO_ORG_ID"] != "941141787" or cfg["CF_CREDITNOTE_ID"] != FIELD:
        raise BackfillError("Organisation or field does not match the approved configuration")
    client = Client(cfg)
    schema = validate_lookup_schema(client.request("GET", "/settings/fields", query={"entity": "creditnote"}), FIELD, "creditnote")
    before = client.read(cn)
    invoice = client.read(inv)
    allocations = client.request("GET", "/creditnotes/" + CN + "/invoices")
    fields = client.request("GET", "/creditnotes/" + CN + "/customfields")
    if FOLDER != ORIGINAL:
        for filename, current in (("before.json", before), ("allocations_before.json", allocations),
                                  ("fields_before.json", fields)):
            if json.loads((ORIGINAL / filename).read_text(encoding="utf-8")) != current:
                raise BackfillError("Fresh evidence differs from the original " + filename)
    credited = allocations.get("invoices_credited")
    if (not before.get("customer_id") or before.get("customer_id") != invoice.get("customer_id")
            or before.get("invoice_id") not in (None, "") or before.get("invoice_number") not in (None, "")
            or before.get("reference_invoice_ids") not in (None, [], "")
            or not isinstance(credited, list) or len(credited) != 1
            or credited[0].get("invoice_id") != INV or credited[0].get("invoice_number") != "152637"
            or field_value(fields) != "" or branch_value(before, FIELD, schema) != ""):
        raise BackfillError("Fresh customer, association, allocation or branch checks failed")
    payload = {"reference_invoice_ids": [INV], "custom_fields": [{"customfield_id": FIELD, "value": cn["branch_record_id"]}]}
    if FOLDER != ORIGINAL:
        old_attempt = json.loads((ORIGINAL / "attempt.json").read_text(encoding="utf-8"))
        old_identity = json.loads((ORIGINAL / "identity.json").read_text(encoding="utf-8"))
        if (old_attempt != {"method": "PUT", "path": "/creditnotes/" + CN, "payload": payload}
                or old_identity != {"organisation_id": cfg["ZOHO_ORG_ID"], "map_sha256": fingerprint, "mapping": cn}):
            raise BackfillError("Original request or identity differs from the current test")
    FOLDER.mkdir()
    save("identity", {"organisation_id": cfg["ZOHO_ORG_ID"], "map_sha256": fingerprint, "mapping": cn})
    save("before", before)
    save("invoice_before", invoice)
    save("allocations_before", allocations)
    save("fields_before", fields)
    save("attempt", {"method": "PUT", "path": "/creditnotes/" + CN, "payload": payload})
    try:
        response = client.request("PUT", "/creditnotes/" + CN, payload)
        save("write_response", response)
    except BackfillError as exc:
        evidence = {"error": str(exc)}
        if isinstance(exc, APIError):
            evidence.update({"http_status": exc.status, "response": exc.body})
        save("write_error", evidence)
        raise BackfillError("Write rejected or uncertain; evidence saved, do not retry") from None
    after = client.read(cn)
    after_allocations = client.request("GET", "/creditnotes/" + CN + "/invoices")
    after_fields = client.request("GET", "/creditnotes/" + CN + "/customfields")
    save("after", after)
    save("allocations_after", after_allocations)
    save("fields_after", after_fields)
    old = protected(before, FIELD, schema)
    new = protected(after, FIELD, schema)
    # The intended association can appear in these representations only.
    for key in ("invoice_id", "invoice_number", "reference_invoice_ids"):
        old.pop(key, None)
        new.pop(key, None)
    differences = document_differences(old, new)
    save("differences", differences)
    association_ok = (after.get("invoice_id") == INV and after.get("invoice_number") == "152637"
                      and after.get("reference_invoice_ids") in (None, "", [INV]))
    ok = (not differences and allocations == after_allocations and association_ok
          and field_value(after_fields) == cn["branch_record_id"])
    save("result", {"verified": ok, "association_verified": association_ok,
                    "allocation_unchanged": allocations == after_allocations,
                    "difference_paths": [d["path"] for d in differences]})
    print(dumps({"creditnote_number": cn["number"], "verified": ok, "evidence_folder": str(FOLDER)}))
    if not ok:
        raise BackfillError("Post-write verification failed; stop and inspect evidence")


if __name__ == "__main__":
    try:
        main()
    except (BackfillError, OSError, ValueError, StopIteration) as error:
        print("Guarded test stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
