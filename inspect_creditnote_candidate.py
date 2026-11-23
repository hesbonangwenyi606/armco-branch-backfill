"""Capture read-only evidence for one proposed credit-note association."""
import csv
import json
from pathlib import Path
import sys
import time

from backfill_customer_branch import APIError, BackfillError, Client, dumps, load_map, settings


def main():
    rows, fingerprint = load_map("data/Armco_Branch_Backfill_API_Map_corrected.csv")
    if len(sys.argv) > 2:
        raise BackfillError("Usage: inspect_creditnote_candidate.py [creditnote_number]")
    with open("output/corrected_run/creditnote_preliminary_candidates.csv", encoding="utf-8", newline="") as f:
        candidates = list(csv.DictReader(f))
    candidate = next((r for r in candidates if r["creditnote_number"] == sys.argv[1]), None) if len(sys.argv) == 2 else next(iter(candidates), None)
    if not candidate:
        raise BackfillError("No preliminary candidate available")
    matches = [r for r in rows if r["type"] == "creditnote" and r["zoho_id"] == candidate["creditnote_id"]]
    invoices = [r for r in rows if r["type"] == "invoice" and r["zoho_id"] == candidate["invoice_id"]]
    if (len(matches) != 1 or len(invoices) != 1 or matches[0]["number"] != candidate["creditnote_number"]
            or matches[0]["branch_record_id"] != candidate["branch_record_id"]
            or invoices[0]["number"] != candidate["applied_invoice_number"]):
        raise BackfillError("Candidate differs from the approved mapping")
    cfg = settings(["creditnote", "invoice"])
    if cfg["ZOHO_ORG_ID"] != "941141787" or cfg["CF_CREDITNOTE_ID"] != "1462401000020819915":
        raise BackfillError("Settings differ from the approved organisation or credit-note field")
    client = Client(cfg)
    def read_stage(name, action):
        try:
            return action()
        except APIError as error:
            code = error.body.get("code") if isinstance(error.body, dict) else None
            suffix = "; Zoho code " + str(code) if type(code) is int else ""
            raise BackfillError(name + " failed: HTTP " + str(error.status) + suffix) from None
    credit = read_stage("Credit-note read", lambda: client.read(matches[0]))
    invoice = read_stage("Invoice read", lambda: client.read(invoices[0]))
    allocations = read_stage("Credit-note allocations read", lambda: client.request("GET", "/creditnotes/" + candidate["creditnote_id"] + "/invoices"))
    fields = read_stage("Credit-note custom-fields read", lambda: client.request("GET", "/creditnotes/" + candidate["creditnote_id"] + "/customfields"))
    folder = Path("output/corrected_run") / ("creditnote_preflight_" + str(time.time_ns()))
    folder.mkdir()
    for name, value in (("candidate", candidate), ("creditnote", credit), ("invoice", invoice),
                        ("allocations", allocations), ("custom_fields", fields), ("identity", {"map_sha256": fingerprint, "organisation_id": cfg["ZOHO_ORG_ID"]})):
        (folder / (name + ".json")).write_text(dumps(value) + "\n", encoding="utf-8")
    same_customer = bool(credit.get("customer_id")) and credit.get("customer_id") == invoice.get("customer_id")
    print(json.dumps({"creditnote_number": candidate["creditnote_number"],
                      "invoice_number": candidate["applied_invoice_number"],
                      "same_customer": same_customer, "evidence_folder": str(folder),
                      "remote_updates_performed": 0}))


if __name__ == "__main__":
    try:
        main()
    except (BackfillError, OSError, ValueError) as error:
        print("Read-only preflight stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
