"""Read-only, bounded search for a mapped credit note with an existing invoice link."""
import argparse
import json
from pathlib import Path
import sys
import time

from backfill_customer_branch import (BackfillError, Client, branch_value, dumps,
    load_map, numeric, settings, validate_lookup_schema)


class ReadOnlyClient(Client):
    def request(self, method, path, payload=None, query=None):
        if method != "GET" or payload is not None:
            raise BackfillError("Candidate search permits document GET requests only")
        return super().request(method, path, payload, query)


def classify(doc, field_id, schema):
    link = doc.get("invoice_id")
    if not numeric(link) or not link.strip("0"):
        return "missing_invoice_association"
    if not isinstance(doc.get("invoice_number"), str) or not doc["invoice_number"].strip():
        return "missing_invoice_number"
    if doc.get("status") == "void":
        return "void_creditnote"
    for container in (doc, doc.get("custom_field_hash", {})):
        if container.get("cf_customer_branch_unformatted") not in (None, ""):
            return "branch_present"
    value = branch_value(doc, field_id, schema)
    return "branch_present" if value else "candidate_requires_guarded_test"


def scan(client, rows, schema, folder, limit):
    records = []
    candidate = None
    for row in rows[:limit]:
        try:
            doc = client.read(row)
            state = classify(doc, schema["field_id"], schema)
        except BackfillError as error:
            # Stop on API errors rather than burning the remaining allowance.
            records.append({"number": row["number"], "document_id": row["zoho_id"],
                            "status": "read_failed", "error": str(error)})
            (folder / "report.json").write_text(dumps(records) + "\n", encoding="utf-8")
            raise
        records.append({"number": row["number"], "document_id": row["zoho_id"], "status": state})
        (folder / "report.json").write_text(dumps(records) + "\n", encoding="utf-8")
        if state == "candidate_requires_guarded_test":
            candidate = {"mapping": row, "invoice_id": doc["invoice_id"],
                         "invoice_number": doc["invoice_number"], "schema": schema}
            (folder / "candidate.json").write_text(dumps(candidate) + "\n", encoding="utf-8")
            (folder / "candidate_document.json").write_text(dumps(doc) + "\n", encoding="utf-8")
            break
        if len(records) % 10 == 0:
            print(dumps({"checked": len(records), "limit": limit, "remote_updates_performed": 0}), flush=True)
    summary = {"checked": len(records), "candidate_found": candidate is not None,
               "remote_updates_performed": 0, "evidence_folder": str(folder)}
    if candidate:
        summary["creditnote_number"] = candidate["mapping"]["number"]
        summary["document_id"] = candidate["mapping"]["zoho_id"]
    else:
        summary["note"] = "No suitable candidate in this bounded sample; no documents changed"
    (folder / "summary.json").write_text(dumps(summary) + "\n", encoding="utf-8")
    print(dumps(summary), flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--skip", type=int, default=0)
    args = parser.parse_args()
    if not 1 <= args.limit <= 200 or args.skip < 0:
        raise BackfillError("Use limit 1..200 and nonnegative skip")
    rows, fingerprint = load_map("data/Armco_Branch_Backfill_API_Map_corrected.csv")
    cfg = settings(["creditnote"])
    if cfg["ZOHO_ORG_ID"] != "941141787" or cfg["CF_CREDITNOTE_ID"] != "1462401000020819915":
        raise BackfillError("Settings differ from approved organisation/field")
    client = ReadOnlyClient(cfg, delay=0)
    schema = validate_lookup_schema(client.request("GET", "/settings/fields",
        query={"entity": "creditnote"}), cfg["CF_CREDITNOTE_ID"], "creditnote")
    selected = [row for row in rows if row["type"] == "creditnote" and
                row["zoho_id"] != "1462401000008001966"][args.skip:]
    folder = Path("output/corrected_run") / ("creditnote_candidate_scan_" + str(time.time_ns()))
    folder.mkdir()
    (folder / "identity.json").write_text(dumps({"map_sha256": fingerprint,
        "organisation_id": cfg["ZOHO_ORG_ID"], "api": cfg["ZOHO_API"],
        "skip": args.skip, "limit": args.limit}) + "\n", encoding="utf-8")
    print("Read-only search started; progress prints every 10 credit notes.", flush=True)
    scan(client, selected, schema, folder, args.limit)


if __name__ == "__main__":
    try:
        main()
    except (BackfillError, OSError, ValueError) as error:
        print("Candidate search stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
