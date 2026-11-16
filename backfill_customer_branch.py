"""One-time, resumable Customer Branch backfill. See README before live use."""
import argparse
import collections
import contextlib
import csv
import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib.parse import urlparse

import requests

EXPECTED = {"invoice": 14450, "creditnote": 2452}
COLUMNS = ("type", "zoho_id", "number", "branch_name", "branch_record_id")
FINAL = ("updated_verified", "already_correct", "conflict")


class BackfillError(Exception):
    pass


class VerificationError(BackfillError):
    pass


class APIError(BackfillError):
    """Keep response evidence locally while the exception text stays redacted."""
    def __init__(self, status, body):
        super().__init__(f"API rejected request (HTTP {status})")
        self.status, self.body = status, body


def dumps(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def numeric(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9]+", value) is not None


def field_diagnostic(doc):
    """Expose response structure, never customer values, to diagnose lookup omissions."""
    def shape(value):
        result = {"type": type(value).__name__}
        if value is None or value == "" or value == [] or value == {}:
            result["empty"] = True
        if isinstance(value, dict):
            result["keys"] = sorted(value)
        if isinstance(value, list):
            result["count"] = len(value)
            result["item_types"] = sorted({type(v).__name__ for v in value})
        return result
    fields = doc.get("custom_fields")
    metadata = []
    if isinstance(fields, list):
        for field in fields:
            if isinstance(field, dict):
                metadata.append({"metadata": {k: field[k] for k in
                    ("customfield_id", "index", "label", "api_name", "data_type") if k in field},
                    "value_shape": shape(field.get("value"))})
    related = {}
    for key, value in doc.items():
        if key != "custom_fields" and any(term in key.lower() for term in ("custom", "branch", "lookup")):
            related[key] = shape(value)
            if isinstance(value, dict):
                related[key]["branch_entries"] = {k: shape(v) for k, v in value.items()
                                                   if "branch" in k.lower()}
    return {"document_keys": sorted(doc), "custom_fields_present": "custom_fields" in doc,
            "custom_fields_shape": shape(fields), "custom_field_metadata": metadata,
            "related_fields": related}


def lookup_metadata(body, field_id):
    fields = body.get("fields")
    if not isinstance(fields, list) or any(not isinstance(field, dict) for field in fields):
        raise BackfillError("Unexpected settings/fields response; expected a fields list")
    matches = [field for field in fields
               if str(field.get("field_id", field.get("customfield_id", ""))) == field_id]
    if len(matches) != 1:
        raise BackfillError("Configured Customer Branch ID absent or duplicated in field metadata")
    field = matches[0]
    # Return schema information only, without default values or lookup records.
    keys = ("field_id", "customfield_id", "index", "label", "api_name", "data_type", "entity",
            "is_active", "is_mandatory", "is_read_only", "is_readonly", "is_editable",
            "show_on_pdf", "related_entity", "lookup_module", "lookup_module_api_name",
            "related_field", "lookup_field", "is_visible", "is_enabled",
            "permission", "visibility", "is_configure_permission", "is_custom_field", "status")
    return {"metadata_keys": sorted(field), "field": {key: field[key] for key in keys
            if key in field and isinstance(field[key], (str, int, float, bool, type(None)))}}


def validate_lookup_schema(body, field_id, module):
    field = lookup_metadata(body, field_id)["field"]
    required = {"api_name": "cf_customer_branch", "data_type": "lookup", "entity": module,
                "related_entity": "cm_customer_branch", "permission": "write", "visibility": "all",
                "is_active": True, "is_read_only": False, "is_custom_field": True}
    for key, value in required.items():
        if key not in field or type(field[key]) is not type(value) or field[key] != value:
            raise BackfillError(f"Lookup schema check failed for {module}: {key}")
    return field


def omitted_lookup_candidate(doc, field_id, schema):
    """Read-only classification only: absence is provisional, never proof of emptiness."""
    fields, hashed = doc.get("custom_fields"), doc.get("custom_field_hash")
    if not isinstance(fields, list) or any(not isinstance(f, dict) for f in fields):
        raise BackfillError("Document has invalid custom_fields")
    if any(str(f.get("customfield_id")) == field_id for f in fields):
        return False
    if not isinstance(hashed, dict):
        raise BackfillError("Omitted lookup requires an explicit custom_field_hash object")
    api_name = schema["api_name"]
    if any(f.get("api_name") == api_name for f in fields):
        raise BackfillError("Lookup API name appears with a different field ID")
    for key in (api_name, api_name + "_formatted"):
        for container in (doc, hashed):
            if key in container and container[key] not in (None, ""):
                raise BackfillError("Lookup omitted from custom_fields but another representation is populated")
    return True


def load_map(path, expected=None):
    raw = Path(path).read_bytes()
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
    if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
        raise BackfillError("Missing or duplicate CSV headers")
    if not set(COLUMNS).issubset(reader.fieldnames):
        raise BackfillError("Required CSV columns: " + ",".join(COLUMNS))
    rows, seen = [], {}
    for line, item in enumerate(reader, 2):
        if None in item or any(item.get(c) is None for c in COLUMNS):
            raise BackfillError(f"Malformed CSV row {line}")
        row = {c: item[c].strip() for c in COLUMNS}
        if row["type"] not in EXPECTED:
            raise BackfillError(f"Invalid document type at row {line}")
        if not numeric(row["zoho_id"]) or not numeric(row["branch_record_id"]):
            raise BackfillError(f"IDs must be digit strings at row {line}")
        if not row["number"] or not row["branch_name"]:
            raise BackfillError(f"Missing number or branch name at row {line}")
        key = (row["type"], row["zoho_id"])
        if key in seen:
            kind = "Conflicting mapping" if seen[key] != row else "Duplicate document"
            raise BackfillError(f"{kind} at row {line}")
        seen[key] = row
        rows.append(row)
    counts = dict(collections.Counter(r["type"] for r in rows))
    if any(counts.get(t, 0) != n for t, n in (expected or EXPECTED).items()):
        raise BackfillError(f"Scope mismatch: {counts}; expected {expected or EXPECTED}")
    return rows, hashlib.sha256(raw).hexdigest()


def settings(types, fields=True):
    names = ["ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN",
             "ZOHO_ORG_ID", "ZOHO_ACCOUNTS", "ZOHO_API"]
    if fields:
        names += ["CF_" + t.upper() + "_ID" for t in types]
    missing = [n for n in names if not os.environ.get(n, "").strip()]
    if missing:
        raise BackfillError("Missing settings: " + ", ".join(missing))
    config = {n: os.environ[n].strip().rstrip("/") for n in names}
    if not numeric(config["ZOHO_ORG_ID"]) or any(
            not numeric(v) for n, v in config.items() if n.startswith("CF_")):
        raise BackfillError("Organisation and custom field IDs must be digit strings")
    for name, prefix in (("ZOHO_ACCOUNTS", "accounts.zoho."), ("ZOHO_API", "www.zohoapis.")):
        parsed = urlparse(config[name])
        # Explicit Zoho hosts only: credentials never go to arbitrary endpoints.
        hosts = {prefix + suffix for suffix in ("com", "eu", "in", "com.au", "jp", "ca", "sa", "com.cn")}
        if parsed.scheme != "https" or parsed.netloc not in hosts or parsed.query or parsed.fragment:
            raise BackfillError(f"Invalid official HTTPS Zoho endpoint: {name}")
        if parsed.path != ("/books/v3" if name == "ZOHO_API" else ""):
            raise BackfillError(f"Unexpected endpoint path: {name}")
    if config["ZOHO_ACCOUNTS"].split("accounts.zoho.")[1] != config["ZOHO_API"].split("www.zohoapis.")[1].split("/")[0]:
        raise BackfillError("Zoho accounts and API data centres differ")
    return config


class Client:
    def __init__(self, config, delay=0.7, retries=3, session=None, sleep=time.sleep,
                 requests_per_minute=85, clock=time.monotonic):
        if not 1 <= requests_per_minute <= 90:
            raise BackfillError("Request-rate target must be 1..90 per minute")
        self.config, self.delay, self.retries = config, delay, retries
        self.session, self.sleep = session or requests.Session(), sleep
        self.token = None
        self.request_interval = 60.0 / requests_per_minute
        self.clock, self.last_request_started = clock, None

    def pace(self):
        wait = self.delay
        if self.last_request_started is not None:
            wait = max(wait, self.last_request_started + self.request_interval - self.clock())
        if wait > 0:
            self.sleep(wait)
        self.last_request_started = self.clock()

    def refresh(self):
        for attempt in range(self.retries + 1):
            try:
                self.pace()
                response = self.session.post(self.config["ZOHO_ACCOUNTS"] + "/oauth/v2/token",
                    data={"grant_type": "refresh_token", "client_id": self.config["ZOHO_CLIENT_ID"],
                          "client_secret": self.config["ZOHO_CLIENT_SECRET"],
                          "refresh_token": self.config["ZOHO_REFRESH_TOKEN"]},
                    timeout=(10, 60), allow_redirects=False)
                if response.status_code == 429 or response.status_code >= 500:
                    self.backoff(response, attempt)
                    continue
                body = response.json()
                if response.status_code != 200 or not isinstance(body, dict) or not body.get("access_token"):
                    raise BackfillError("OAuth refresh rejected; check credentials and data centre")
                self.token = body["access_token"]
                return
            except (requests.RequestException, ValueError):
                self.sleep(min(2 ** attempt, 30))
        raise BackfillError("OAuth refresh failed after bounded retries")

    def backoff(self, response, attempt):
        try:
            wait = float(response.headers.get("Retry-After", 2 ** attempt))
        except (ValueError, TypeError):
            wait = 2 ** attempt
        if not math.isfinite(wait) or wait > 300:
            raise BackfillError("Rate limit requires a later restart")
        self.sleep(max(0, wait))

    def request(self, method, path, payload=None, query=None):
        params = dict(query or {})
        params["organization_id"] = self.config["ZOHO_ORG_ID"]
        if not self.token:
            self.refresh()
        renewed = False
        last_read_rejection = None
        for attempt in range(self.retries + 1):
            try:
                self.pace()
                response = self.session.request(method, self.config["ZOHO_API"] + path,
                    params=params,
                    headers={"Authorization": "Zoho-oauthtoken " + self.token},
                    json=payload, timeout=(10, 60), allow_redirects=False)
            except requests.RequestException:
                if method != "GET":
                    raise BackfillError("Uncertain write: network failure; read before retry") from None
                self.sleep(min(2 ** attempt, 30))
                continue
            if response.status_code == 401 and not renewed:
                if method != "GET":
                    # A 401 can mean missing OAuth scope, not just expiry. Keep
                    # server evidence without retrying or mislabelling the cause.
                    try:
                        rejected = response.json()
                    except ValueError:
                        rejected = {"message": "Non-JSON authentication rejection"}
                    raise APIError(response.status_code, rejected)
                self.refresh()
                renewed = True
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if method == "GET":
                    try:
                        rejected = response.json()
                    except ValueError:
                        rejected = {"message": "Non-JSON temporary API rejection"}
                    last_read_rejection = APIError(response.status_code, rejected)
                self.backoff(response, attempt)
                if method != "GET":
                    raise BackfillError("Uncertain write: temporary API failure; reconcile before retry")
                continue
            try:
                body = response.json()
            except ValueError:
                raise BackfillError("Invalid API JSON; reconcile any pending write") from None
            if not isinstance(body, dict) or response.status_code != 200 or body.get("code") != 0:
                # Never echo response bodies: they may contain credentials or customer data.
                raise APIError(response.status_code, body)
            return body
        if last_read_rejection is not None:
            raise last_read_rejection
        raise BackfillError("Read failed after bounded retries")

    def read(self, row):
        t = row["type"]
        body = self.request("GET", f"/{t}s/{row['zoho_id']}")
        doc = body.get(t)
        if not isinstance(doc, dict) or str(doc.get(t + "_id")) != row["zoho_id"]:
            raise BackfillError("API returned missing or mismatched document ID")
        if doc.get(t + "_number") != row["number"]:
            raise BackfillError("Document number differs from approved map")
        return doc

    def write(self, row, field_id, doc):
        if row["type"] == "invoice":
            method, path = "PUT", f"/invoice/{row['zoho_id']}/customfields"
            field = {"customfield_id": field_id, "value": row["branch_record_id"]}
        else:
            method, path = "POST", f"/creditnotes/{row['zoho_id']}/customfields"
            found = find_field(doc, field_id)
            index = found.get("index")
            if isinstance(index, bool) or not str(index).isdigit() or not 1 <= int(index) <= 46:
                raise BackfillError("Credit note discovery must supply Customer Branch index (1..46)")
            field = {"index": int(index), "value": row["branch_record_id"]}
        return self.request(method, path, {"custom_fields": [field]})


def find_field(doc, field_id, empty_schema=None):
    fields = doc.get("custom_fields")
    if not isinstance(fields, list) or any(not isinstance(f, dict) for f in fields):
        raise BackfillError("Document has invalid custom_fields")
    matches = [f for f in fields if str(f.get("customfield_id")) == field_id]
    if not matches and empty_schema and omitted_lookup_candidate(doc, field_id, empty_schema):
        # Only a guarded, explicitly UI-confirmed one-invoice test supplies this.
        # Do not alter the original response saved in SQLite.
        return {"customfield_id": field_id, "api_name": empty_schema["api_name"], "value": ""}
    if len(matches) != 1:
        raise BackfillError("Customer Branch field absent or duplicated; run discovery")
    return matches[0]


def branch_value(doc, field_id, empty_schema=None):
    field = find_field(doc, field_id, empty_schema)
    if "value" not in field:
        raise BackfillError("Customer Branch entry lacks a value; cannot infer emptiness")
    value = field["value"]
    if value is None or value == "":
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not numeric(str(value)):
        raise BackfillError("Unrecognised lookup value shape; confirm live API representation")
    return str(value)


def protected(doc, field_id, empty_schema=None):
    """Compare all returned business data; ignore only known update/branch mirrors."""
    field = find_field(doc, field_id, empty_schema)
    result = json.loads(dumps(doc))
    for key in ("last_modified_time", "updated_time"):
        result.pop(key, None)
    result["custom_fields"] = sorted(
        [f for f in result["custom_fields"] if str(f.get("customfield_id")) != field_id],
        key=lambda f: str(f.get("customfield_id", f.get("index", ""))))
    # Zoho may also return the same field as a top-level API-name mirror/hash.
    api_name = (empty_schema or {}).get("api_name") or field.get("api_name")
    if api_name:
        result.pop(api_name, None)
        result.pop(api_name + "_formatted", None)
    hashed = result.get("custom_field_hash")
    if isinstance(hashed, dict) and api_name:
        hashed.pop(api_name, None)
        hashed.pop(api_name + "_formatted", None)
    return result


def verify_snapshot(original, current, field_id, empty_schema=None):
    try:
        if protected(original, field_id, empty_schema) != protected(current, field_id, empty_schema):
            raise VerificationError("Protected document data changed since original snapshot")
        return branch_value(current, field_id, empty_schema)
    except BackfillError as exc:
        raise VerificationError(str(exc)) from None


def document_differences(before, after, path=""):
    """JSON pointer paths plus exact local values; callers print paths only."""
    changes = []
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            child = path + "/" + key.replace("~", "~0").replace("/", "~1")
            if key not in before or key not in after:
                changes.append({"path": child, "before_present": key in before,
                                "after_present": key in after, "before": before.get(key), "after": after.get(key)})
            else:
                changes.extend(document_differences(before[key], after[key], child))
    elif isinstance(before, list) and isinstance(after, list):
        for index in range(max(len(before), len(after))):
            child = path + "/" + str(index)
            if index >= len(before) or index >= len(after):
                changes.append({"path": child, "before_present": index < len(before),
                                "after_present": index < len(after),
                                "before": before[index] if index < len(before) else None,
                                "after": after[index] if index < len(after) else None})
            else:
                changes.extend(document_differences(before[index], after[index], child))
    elif type(before) is not type(after) or before != after:
        changes.append({"path": path or "/", "before_present": True, "after_present": True,
                        "before": before, "after": after})
    return changes


def audit_saved_document(client, folder, module, identifier, identity):
    """Read current Zoho state and retain evidence; never write Zoho or clear a halt."""
    database = folder / "progress.sqlite3"
    if not database.is_file():
        raise BackfillError("No existing progress database to audit")
    with contextlib.closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        saved_identity = db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()
        if not saved_identity or json.loads(saved_identity[0]) != identity:
            raise BackfillError("Audit map/settings differ from saved run identity")
        saved = db.execute("SELECT * FROM documents WHERE type=? AND zoho_id=?", (module, identifier)).fetchone()
        if not saved or not saved["original"]:
            raise BackfillError("No original pre-write snapshot for the selected document")
        confirmation = db.execute("SELECT value FROM meta WHERE key='invoice_ui_empty_confirmation'").fetchone()
        halted = db.execute("SELECT value FROM meta WHERE key='halted'").fetchone()
    original, row = json.loads(saved["original"]), json.loads(saved["mapping"])
    schema = None
    if module == "invoice" and confirmation:
        confirmed = json.loads(confirmation[0])
        if confirmed.get("document_id") == identifier:
            schema = confirmed.get("schema")
    field_id = identity["CF_" + module.upper() + "_ID"]
    if schema:
        validate_lookup_schema({"fields": [schema]}, field_id, module)
    current = client.read(row)
    raw_changes = document_differences(original, current)
    # Save the actual fresh response even if normalization cannot interpret it.
    destination = folder / ("audit_" + str(time.time_ns()))
    destination.mkdir()
    (destination / "original.json").write_text(dumps(original) + "\n", encoding="utf-8")
    (destination / "current.json").write_text(dumps(current) + "\n", encoding="utf-8")
    normalization_error = None
    branch_read_error = None
    try:
        protected_changes = document_differences(protected(original, field_id, schema),
                                                 protected(current, field_id, schema))
    except BackfillError as error:
        protected_changes = []
        normalization_error = str(error)
    try:
        expected_matches = branch_value(current, field_id) == row["branch_record_id"]
    except BackfillError as error:
        expected_matches = None
        branch_read_error = str(error)
    evidence = {"document_type": module, "document_id": identifier,
                "checked_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "saved_status": saved["status"], "saved_error": saved["error"],
                "halt_retained": bool(halted and json.loads(halted[0])),
                "expected_branch_matches": expected_matches, "normalization_error": normalization_error,
                "branch_read_error": branch_read_error,
                "protected_changes": protected_changes, "raw_changes": raw_changes,
                "note": "Fresh audit read, not the response at the time of the initial verification failure. No remote writes or halt changes."}
    (destination / "audit.json").write_text(dumps(evidence) + "\n", encoding="utf-8")
    print(dumps({"expected_branch_matches": expected_matches, "normalization_error": normalization_error,
                 "branch_read_error": branch_read_error,
                 "protected_changed_paths": [change["path"] for change in protected_changes],
                 "raw_changed_paths": [change["path"] for change in raw_changes],
                 "evidence_folder": str(destination), "halt_retained": evidence["halt_retained"]}))
    return 0


class Store:
    def __init__(self, path, rows, identity):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS documents (
                type TEXT, zoho_id TEXT, mapping TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                original TEXT, error TEXT NOT NULL DEFAULT '', test_verified INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(type, zoho_id));
        """)
        saved = self.get("identity")
        if saved is not None and saved != identity:
            self.db.close()
            raise BackfillError("Run identity changed: map, organisation, endpoints or field IDs differ")
        with self.db:
            self.put("identity", identity)
            self.db.executemany("INSERT OR IGNORE INTO documents(type,zoho_id,mapping) VALUES(?,?,?)",
                                [(r["type"], r["zoho_id"], dumps(r)) for r in rows])

    def get(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, dumps(value)))

    def update(self, row, status, error="", original=None, test=False):
        with self.db:
            self.db.execute("""UPDATE documents SET status=?,error=?,original=COALESCE(original,?),
                test_verified=MAX(test_verified,?) WHERE type=? AND zoho_id=?""",
                (status, error, dumps(original) if original is not None else None,
                 int(test), row["type"], row["zoho_id"]))
        logging.info("%s %s %s", row["type"], row["zoho_id"], status)

    def halt(self, reason):
        with self.db:
            self.put("halted", reason)

    def record_evidence(self, row, kind, value):
        key = f"evidence:{row['type']}:{row['zoho_id']}:{time.time_ns()}:{kind}"
        with self.db:
            self.put(key, value)

    def report(self, folder):
        return report_rows(self.db.execute("SELECT * FROM documents ORDER BY type,zoho_id"), folder)


def report_rows(saved_rows, folder):
    """Export a single saved-row snapshot without requiring a writable database."""
    counts = {s: 0 for s in ("updated_verified", "already_correct", "conflict", "failed", "pending")}
    records = []
    for saved in saved_rows:
        row = json.loads(saved["mapping"])
        status = saved["status"]
        category = "pending" if status in ("ready", "ready_omitted", "updating") else status
        counts[category] += 1
        records.append({**row, "status": category, "detail": status,
                        "error": saved["error"], "test_verified": saved["test_verified"]})
    counts["total_mapped"] = len(records)
    assert sum(counts[s] for s in counts if s != "total_mapped") == len(records)
    with open(Path(folder) / "report.csv", "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(COLUMNS) + ["status", "detail", "error", "test_verified"])
        writer.writeheader()
        # Prevent spreadsheet formula execution when opening customer strings.
        writer.writerows({k: ("'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")) else v)
                          for k, v in r.items()} for r in records)
    (Path(folder) / "summary.json").write_text(dumps(counts) + "\n", encoding="utf-8")
    return counts


def process(client, store, rows, config, dry=False, test=False, limit=None, max_failures=5,
            omitted_schemas=None, resume_dry=False, omitted_test_schema=None):
    if omitted_test_schema is not None:
        if not test or dry or limit != 1 or len(rows) != 1 or rows[0]["type"] != "invoice":
            raise BackfillError("UI-confirmed omission handling is restricted to one explicit invoice test")
        validate_lookup_schema({"fields": [omitted_test_schema]}, config["CF_INVOICE_ID"], "invoice")
    if resume_dry and not dry:
        raise BackfillError("Dry-run resume is available only in read-only mode")
    if omitted_schemas and not dry:
        raise BackfillError("Omitted-lookup classification is read-only; live updates remain blocked")
    if store.get("halted"):
        raise VerificationError("Run halted for investigation; consult SQLite halted metadata and README")
    failures, processed, verified_tests = 0, 0, 0
    for row in rows:
        saved = store.db.execute("SELECT * FROM documents WHERE type=? AND zoho_id=?",
                                 (row["type"], row["zoho_id"])).fetchone()
        if saved["status"] in FINAL:
            continue
        if dry and resume_dry and (saved["status"] == "ready" or
                (saved["status"] == "ready_omitted" and row["type"] in (omitted_schemas or {}))):
            continue
        if limit is not None and processed >= limit:
            break
        processed += 1
        field_id = config["CF_" + row["type"].upper() + "_ID"]
        try:
            doc = client.read(row)
            original = json.loads(saved["original"]) if saved["original"] else None
            schema = (omitted_schemas or {}).get(row["type"])
            if schema and original is None and omitted_lookup_candidate(doc, field_id, schema):
                store.update(row, "ready_omitted", "Provisional: lookup omitted; live representation test required before writes")
                failures = 0
                continue
            value = (verify_snapshot(original, doc, field_id, omitted_test_schema) if original is not None
                     else branch_value(doc, field_id, omitted_test_schema))
            if original is not None:
                if value == row["branch_record_id"]:
                    store.update(row, "updated_verified", test=test)
                    verified_tests += int(test)
                    failures = 0
                    continue
                if value != branch_value(original, field_id, omitted_test_schema):
                    raise VerificationError("Branch changed unexpectedly after an attempted write")
            if value == row["branch_record_id"]:
                store.update(row, "already_correct")
            elif value:
                store.update(row, "conflict", "Existing branch differs from map")
            elif dry:
                store.update(row, "ready")
            else:
                # Commit original and updating state BEFORE sending any network write.
                store.update(row, "updating", original=doc)
                try:
                    response = client.write(row, field_id, doc)
                except BackfillError as write_error:
                    store.record_evidence(row, "write_error", {"error": str(write_error),
                        "http_status": getattr(write_error, "status", None),
                        "response_body": getattr(write_error, "body", None)})
                    # A timeout or rejection may follow a committed update. Never blindly resend.
                    after = client.read(row)
                    store.record_evidence(row, "readback", after)
                    after_value = verify_snapshot(doc, after, field_id, omitted_test_schema)
                    if after_value not in (branch_value(doc, field_id, omitted_test_schema), row["branch_record_id"]):
                        raise VerificationError("Unexpected branch after uncertain write")
                    if after_value != row["branch_record_id"]:
                        raise BackfillError("Write not verified; restart will read before retry") from None
                else:
                    store.record_evidence(row, "write_response", response)
                    after = client.read(row)
                    store.record_evidence(row, "readback", after)
                if verify_snapshot(doc, after, field_id, omitted_test_schema) != row["branch_record_id"]:
                    raise VerificationError("Customer Branch failed read-back verification")
                store.update(row, "updated_verified", test=test)
                verified_tests += int(test)
            failures = 0
        except VerificationError as exc:
            store.update(row, "failed", str(exc))
            store.halt(str(exc))
            raise
        except BackfillError as exc:
            store.update(row, "failed", str(exc))
            failures += 1
            if failures >= max_failures:
                raise BackfillError("Stopped after repeated document failures") from None
    return verified_tests


@contextlib.contextmanager
def run_lock(folder):
    # OS lock is released even after a crash; the persistent file is harmless.
    file = open(Path(folder) / "run.lock", "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            file.seek(0, os.SEEK_END)
            if file.tell() == 0:
                file.write(b"0")
                file.flush()
            file.seek(0)
            try:
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise BackfillError("Another process is using this output directory") from None
        else:
            import fcntl
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise BackfillError("Another process is using this output directory") from None
        yield
    finally:
        file.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--validate", action="store_true")
    mode.add_argument("--discover", metavar="DOCUMENT_ID")
    mode.add_argument("--diagnose", metavar="DOCUMENT_ID", help="Read-only field structure diagnostic, with values omitted")
    mode.add_argument("--field-metadata", action="store_true", help="Read Customer Branch field settings (requires settings.READ)")
    mode.add_argument("--audit-document", metavar="DOCUMENT_ID", help="Read-only comparison against saved pre-write snapshot; keeps halt")
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--test", type=int, choices=[1])
    mode.add_argument("--run", action="store_true")
    mode.add_argument("--report", action="store_true")
    mode.add_argument("--approve-test", choices=list(EXPECTED))
    parser.add_argument("--review-note", help="Manual Zoho UI test verification reference")
    parser.add_argument("--type", choices=list(EXPECTED))
    parser.add_argument("--document-id", help="Select one mapped document for an explicit live test")
    parser.add_argument("--csv", type=Path, default=Path("data/Armco_Branch_Backfill_API_Map.csv"))
    parser.add_argument("--output", type=Path, default=Path("output"))
    parser.add_argument("--delay", type=float, default=0.7)
    parser.add_argument("--requests-per-minute", type=int, default=85,
                        help="Per-process request-start cap, 1..90 (default 85), including retries/OAuth")
    parser.add_argument("--resume-dry-run", action="store_true",
                        help="Skip previously reviewed ready/provisional rows in this dry-run database")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--max-failures", type=int, default=5)
    parser.add_argument("--review-omitted-lookups", action="store_true",
                        help="Dry-run only: classify omitted lookup fields provisionally after schema checks")
    parser.add_argument("--confirm-empty-in-ui", action="store_true",
                        help="One explicit invoice test only: operator has checked that Customer Branch is blank in Zoho UI")
    args = parser.parse_args(argv)
    if not math.isfinite(args.delay) or args.delay < 0 or not 1 <= args.retries <= 10 or args.max_failures < 1:
        parser.error("delay must be finite and nonnegative; retries 1..10; max-failures positive")
    if (args.test or args.discover or args.diagnose or args.field_metadata or args.audit_document) and not args.type:
        parser.error("Document test/discovery/diagnostic/metadata/audit modes require --type")
    if args.audit_document and not numeric(args.audit_document):
        parser.error("Audit document ID must be digits")
    if args.document_id and (not args.test or not numeric(args.document_id)):
        parser.error("--document-id requires --test and a digit ID")
    if args.review_omitted_lookups and not args.dry_run:
        parser.error("--review-omitted-lookups is available only with --dry-run")
    if args.resume_dry_run and not args.dry_run:
        parser.error("--resume-dry-run requires --dry-run")
    if not 1 <= args.requests_per_minute <= 90:
        parser.error("--requests-per-minute must be 1..90")
    if args.confirm_empty_in_ui and (not args.test or args.type != "invoice" or not args.document_id
                                    or not args.review_note or not args.review_note.strip()):
        parser.error("--confirm-empty-in-ui requires --type invoice --test 1 --document-id and --review-note")
    if args.field_metadata:
        cfg = settings([args.type])
        field_id = cfg["CF_" + args.type.upper() + "_ID"]
        body = Client(cfg, args.delay, args.retries, requests_per_minute=args.requests_per_minute).request("GET", "/settings/fields",
            query={"entity": args.type, "field_ids": field_id})
        print(json.dumps(lookup_metadata(body, field_id), indent=2))
        return 0
    if args.discover or args.diagnose:
        identifier = args.discover or args.diagnose
        if not numeric(identifier):
            parser.error("Discovery ID must be digits")
        cfg = settings([args.type], fields=False)
        client = Client(cfg, args.delay, args.retries, requests_per_minute=args.requests_per_minute)
        body = client.request("GET", f"/{args.type}s/{identifier}")
        doc = body.get(args.type)
        if not isinstance(doc, dict) or str(doc.get(args.type + "_id")) != identifier:
            raise BackfillError("Discovery returned mismatched document")
        print(json.dumps(field_diagnostic(doc) if args.diagnose else doc.get("custom_fields", []), indent=2))
        return 0
    rows, fingerprint = load_map(args.csv)
    if args.validate:
        print(dumps({"sha256": fingerprint, "counts": EXPECTED, "total": len(rows)}))
        return 0
    cfg = settings(EXPECTED)
    identity = {n: cfg[n] for n in ("ZOHO_ORG_ID", "ZOHO_ACCOUNTS", "ZOHO_API", "CF_INVOICE_ID", "CF_CREDITNOTE_ID")}
    identity["sha256"] = fingerprint
    identity["protocol"] = 1
    if args.audit_document:
        return audit_saved_document(Client(cfg, args.delay, args.retries,
                                    requests_per_minute=args.requests_per_minute),
                                    args.output, args.type, args.audit_document, identity)
    args.output.mkdir(parents=True, exist_ok=True)
    with run_lock(args.output):
        logging.basicConfig(filename=args.output / "backfill.log", level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s")
        store = Store(args.output / "progress.sqlite3", rows, identity)
        try:
            if args.approve_test:
                if store.get("halted") or not args.review_note or not args.review_note.strip():
                    raise BackfillError("Unhalted run and --review-note required for manual test approval")
                passed = store.db.execute("SELECT COUNT(*) FROM documents WHERE type=? AND test_verified=1 AND status='updated_verified'",
                                         (args.approve_test,)).fetchone()[0]
                if not passed:
                    raise BackfillError("No changed and API-verified live test for this module")
                with store.db:
                    store.put("approved_" + args.approve_test, args.review_note.strip())
            elif not args.report:
                if args.run and not all(store.get("approved_" + t) for t in EXPECTED):
                    raise BackfillError("Full run requires API-verified and manually approved tests for both modules")
                selected = [r for r in rows if not args.type or r["type"] == args.type]
                if args.document_id:
                    selected = [r for r in selected if r["zoho_id"] == args.document_id]
                    if not selected:
                        raise BackfillError("Test document is outside the approved map")
                client = Client(cfg, args.delay, args.retries, requests_per_minute=args.requests_per_minute)
                omitted_schemas = None
                omitted_test_schema = None
                if args.confirm_empty_in_ui:
                    field_id = cfg["CF_INVOICE_ID"]
                    body = client.request("GET", "/settings/fields", query={"entity": "invoice", "field_ids": field_id})
                    omitted_test_schema = validate_lookup_schema(body, field_id, "invoice")
                    with store.db:
                        store.put("invoice_ui_empty_confirmation", {"document_id": args.document_id,
                                  "review_note": args.review_note.strip(), "schema": omitted_test_schema})
                if args.review_omitted_lookups:
                    omitted_schemas = {}
                    for module in sorted({r["type"] for r in selected}):
                        field_id = cfg["CF_" + module.upper() + "_ID"]
                        body = client.request("GET", "/settings/fields", query={"entity": module, "field_ids": field_id})
                        omitted_schemas[module] = validate_lookup_schema(body, field_id, module)
                    with store.db:
                        store.put("omitted_review_schemas", omitted_schemas)
                verified = process(client, store, selected, cfg,
                                   dry=args.dry_run, test=bool(args.test), limit=args.test,
                                   max_failures=args.max_failures, omitted_schemas=omitted_schemas,
                                   resume_dry=args.resume_dry_run, omitted_test_schema=omitted_test_schema)
                if args.test and not verified:
                    raise BackfillError("Test did not change and verify a document; review report before selecting another")
        finally:
            print(dumps(store.report(args.output)))
            store.db.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (BackfillError, OSError, UnicodeError, csv.Error, sqlite3.Error) as error:
        # OS exception messages can expose paths but never print credentials or API bodies.
        print("Stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("Interrupted; progress saved. Resume with the same command.", file=sys.stderr)
        sys.exit(130)
