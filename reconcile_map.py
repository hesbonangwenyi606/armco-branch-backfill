"""Read-only Zoho ID reconciliation; never overwrites the approved source map."""
import argparse
import collections
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import re
import sqlite3
import sys

from backfill_customer_branch import (
    BackfillError, Client, COLUMNS, EXPECTED, dumps, load_map, numeric, run_lock, settings,
)


class ReadOnlyClient(Client):
    def request(self, method, path, payload=None, query=None):
        if method != "GET" or payload is not None:
            raise BackfillError("Reconciliation permits GET requests only (apart from OAuth token exchange)")
        return super().request(method, path, query=query)


def source_map(path, expected=None):
    """Accept damaged IDs but require an unambiguous approved number/branch scope."""
    raw = Path(path).read_bytes()
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
    if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames) or not set(COLUMNS).issubset(reader.fieldnames):
        raise BackfillError("Missing or duplicate source CSV columns")
    rows, seen = [], set()
    for line, item in enumerate(reader, 2):
        if None in item or any(item.get(c) is None for c in COLUMNS):
            raise BackfillError(f"Malformed source CSV row {line}")
        row = {c: item[c].strip() for c in COLUMNS}
        if row["type"] not in EXPECTED or not row["number"] or not row["branch_name"]:
            raise BackfillError(f"Missing source type, number or branch at row {line}")
        key = row["type"], row["number"]
        if key in seen:
            raise BackfillError(f"Duplicate document number within a module at source row {line}")
        seen.add(key)
        rows.append(row)
    counts = collections.Counter(r["type"] for r in rows)
    if any(counts[t] != n for t, n in (expected or EXPECTED).items()):
        raise BackfillError(f"Source scope counts differ from approval: {dict(counts)}")
    return rows, hashlib.sha256(raw).hexdigest()


class PageCache:
    """Checkpoint successful pages so a token/quota/network interruption can resume."""
    def __init__(self, path, identity):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS identity (value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS pages (path TEXT, page INTEGER, body TEXT NOT NULL,
              PRIMARY KEY(path,page));
        """)
        previous = self.db.execute("SELECT value FROM identity").fetchone()
        if previous and json.loads(previous[0]) != identity:
            self.db.close()
            raise BackfillError("Reconciliation identity changed; retain this evidence and use a new --output directory")
        if not previous:
            with self.db:
                self.db.execute("INSERT INTO identity VALUES(?)", (dumps(identity),))

    def page(self, client, path, page):
        saved = self.db.execute("SELECT body FROM pages WHERE path=? AND page=?", (path, page)).fetchone()
        if saved:
            return json.loads(saved[0])
        body = client.request("GET", path, query={"page": page, "per_page": 200,
                              "sort_column": "created_time", "sort_order": "A", "filter_by": "Status.All"})
        # Validation occurs before caching in scan().
        return body

    def save(self, path, page, body):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO pages VALUES(?,?,?)", (path, page, dumps(body)))


def scan(client, cache, path, list_key, max_pages=1000):
    fingerprints = set()
    for page in range(1, max_pages + 1):
        body = cache.page(client, path, page)
        items, context = body.get(list_key), body.get("page_context")
        if not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
            raise BackfillError(f"Unexpected {list_key} list shape; no candidate map will be published")
        if not isinstance(context, dict) or type(context.get("has_more_page")) is not bool:
            raise BackfillError("Missing explicit pagination completion flag")
        if context.get("page") != page:
            raise BackfillError("API returned an unexpected page number")
        fingerprint = hashlib.sha256(dumps(items).encode()).hexdigest()
        if items and fingerprint in fingerprints:
            raise BackfillError("API repeated a page; stopped to prevent incomplete reconciliation")
        fingerprints.add(fingerprint)
        if not items and context["has_more_page"]:
            raise BackfillError("Empty API page claims more records")
        cache.save(path, page, body)
        print(f"Read {path}: page {page}, {len(items)} records", flush=True)
        yield from items
        if not context["has_more_page"]:
            return
    raise BackfillError("Reached page limit before complete listing; resume with a larger --max-pages")


def document_index(records, module, wanted):
    result = collections.defaultdict(dict)
    for record in records:
        number = record.get(module + "_number")
        if not isinstance(number, str):
            raise BackfillError("Document number from API must be a string")
        if number not in wanted:
            continue
        identifier = record.get(module + "_id")
        if isinstance(identifier, bool) or not numeric(str(identifier)):
            raise BackfillError("Invalid document ID in API listing")
        customer_id = record.get("customer_id")
        if not numeric(str(customer_id)) or isinstance(customer_id, bool):
            raise BackfillError("Missing customer ID in document listing")
        evidence = {"zoho_id": str(identifier), "customer_id": str(customer_id),
                    "customer_name": record.get("customer_name", ""), "date": record.get("date", "")}
        previous = result[number].get(str(identifier))
        if previous is not None and previous != evidence:
            raise BackfillError("A document changed between list pages; review before restarting with fresh evidence")
        result[number][str(identifier)] = evidence
    return result


def branch_index(records, name_field):
    result = collections.defaultdict(set)
    for record in records:
        name, identifier = record.get(name_field), record.get("module_record_id")
        if not isinstance(name, str) or not name.strip() or isinstance(identifier, bool) or not numeric(str(identifier)):
            raise BackfillError("Unexpected branch name/ID shape; specify the actual --branch-name-field")
        result[name].add(str(identifier))
    return result


def reconcile(rows, indexes, branches=None):
    reports, candidate = [], []
    for line, row in enumerate(rows, 2):
        matches = indexes[row["type"]].get(row["number"], {})
        status = "matched" if len(matches) == 1 else ("document_missing" if not matches else "document_ambiguous")
        doc = next(iter(matches.values())) if len(matches) == 1 else {}
        ids = branches.get(row["branch_name"], set()) if branches is not None else set()
        branch_status = ("branch_unverified" if branches is None else
                         "matched" if len(ids) == 1 else "branch_missing" if not ids else "branch_ambiguous")
        branch_id = next(iter(ids)) if len(ids) == 1 else ""
        report = {"source_row": line, **row, "source_zoho_id": row["zoho_id"],
                  "resolved_zoho_id": doc.get("zoho_id", ""), "document_status": status,
                  "resolved_branch_record_id": branch_id, "branch_status": branch_status,
                  "customer_id": doc.get("customer_id", ""), "customer_name": doc.get("customer_name", ""),
                  "date": doc.get("date", "")}
        reports.append(report)
        if status == "matched" and branch_status == "matched":
            candidate.append({**row, "zoho_id": doc["zoho_id"], "branch_record_id": branch_id})
    return reports, candidate


def write_csv(path, rows, fields, protect=True):
    with Path(path).open("x", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            if protect:
                row = {k: ("'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")) else v)
                       for k, v in row.items()}
            writer.writerow(row)


def discover_branch_module(client):
    body = client.request("GET", "/settings/modules")
    modules = body.get("modules")
    if not isinstance(modules, list):
        raise BackfillError("Unexpected custom-module discovery response")
    matches = set()
    for module in modules:
        if not isinstance(module, dict):
            raise BackfillError("Unexpected custom-module metadata")
        labels = [module.get(k, "") for k in ("module_name", "singular_name", "plural_name")]
        if any(str(label).strip().casefold() in ("customer branch", "customer branches") for label in labels):
            name = module.get("module_api_name", "")
            if re.fullmatch(r"cm_[a-zA-Z0-9_]+", name):
                matches.add(name)
    if len(matches) != 1:
        raise BackfillError("Customer Branch module could not be identified uniquely")
    return matches.pop()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=Path("data/Armco_Branch_Backfill_API_Map.csv"))
    parser.add_argument("--output", type=Path, default=Path("output/reconciliation"))
    parser.add_argument("--verify-branches", action="store_true", help="Also read Customer Branch custom-module records")
    parser.add_argument("--branch-module", help="Exact cm_ API name; omitted means auto-discover")
    parser.add_argument("--branch-name-field", default="record_name", help="API field containing the exact approved branch name")
    parser.add_argument("--delay", type=float, default=0.7)
    parser.add_argument("--max-pages", type=int, default=1000)
    args = parser.parse_args(argv)
    if not math.isfinite(args.delay) or args.delay < 0 or args.max_pages < 1:
        parser.error("Use a finite nonnegative delay and positive max-pages")
    if args.branch_module and (not args.verify_branches or not re.fullmatch(r"cm_[a-zA-Z0-9_]+", args.branch_module)):
        parser.error("--branch-module requires --verify-branches and an exact cm_ API name")
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]*", args.branch_name_field):
        parser.error("Invalid branch name API field")
    rows, fingerprint = source_map(args.csv)
    config = settings([], fields=False)
    identity = {k: config[k] for k in ("ZOHO_ORG_ID", "ZOHO_ACCOUNTS", "ZOHO_API")}
    identity.update(source_sha256=fingerprint, protocol=1)
    args.output.mkdir(parents=True, exist_ok=True)
    with run_lock(args.output):
        cache = PageCache(args.output / "read_cache.sqlite3", identity)
        try:
            client = ReadOnlyClient(config, delay=args.delay)
            indexes = {}
            for module in EXPECTED:
                wanted = {r["number"] for r in rows if r["type"] == module}
                indexes[module] = document_index(scan(client, cache, "/" + module + "s", module + "s", args.max_pages), module, wanted)
            branches, branch_error, branch_module = None, None, args.branch_module
            if args.verify_branches:
                try:
                    branch_module = branch_module or discover_branch_module(client)
                    branches = branch_index(scan(client, cache, "/" + branch_module, "module_record", args.max_pages), args.branch_name_field)
                except BackfillError as error:
                    branch_error = str(error)
            reports, candidate = reconcile(rows, indexes, branches)
            summary = {**identity, "generated_utc": datetime.now(timezone.utc).isoformat(),
                       "total_source_rows": len(rows), "branch_module": branch_module,
                       "branch_name_field": args.branch_name_field,
                       "document_status": dict(collections.Counter(r["document_status"] for r in reports)),
                       "branch_status": dict(collections.Counter(r["branch_status"] for r in reports)),
                       "document_ids_changed": sum(r["resolved_zoho_id"] != r["zoho_id"] for r in reports if r["document_status"] == "matched"),
                       "branch_ids_changed": sum(r["resolved_branch_record_id"] != r["branch_record_id"] for r in reports if r["branch_status"] == "matched"),
                       "branch_error": branch_error,
                       "review_required": "Verify document/customer/branch associations and exclusions before approving any candidate."}
            # Exclusive, unique run artifacts avoid stale candidate maps after failed reruns.
            folder = args.output / datetime.now(timezone.utc).strftime("review_%Y%m%dT%H%M%S_%fZ")
            folder.mkdir()
            write_csv(folder / "document_matches.csv", reports, list(reports[0]))
            complete = len(candidate) == len(rows)
            if complete:
                path = folder / "candidate_map.csv"
                write_csv(path, candidate, COLUMNS, protect=False)
                try:
                    load_map(path)
                except BackfillError:
                    path.unlink()
                    raise
                summary["candidate_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            summary["candidate_created"] = complete
            (folder / "summary.json").write_text(dumps(summary) + "\n", encoding="utf-8")
            print("Review folder:", folder)
            print("Document matches:", summary["document_status"])
            print("Branch matches:", summary["branch_status"])
            print("Candidate map created:", complete)
            if branch_error:
                print("Branch verification blocked:", branch_error)
                print("Automatic discovery needs settings.READ; record access is documented as custommodules.ALL.")
            print("Original CSV unchanged. No invoices, credit notes or branch records were updated.")
            return 0 if all(r["document_status"] == "matched" for r in reports) and (not args.verify_branches or complete) else 1
        finally:
            cache.db.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (BackfillError, OSError, UnicodeError, csv.Error, sqlite3.Error) as error:
        print("Stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("Interrupted; successful read pages cached. Resume with the same command.", file=sys.stderr)
        sys.exit(130)
