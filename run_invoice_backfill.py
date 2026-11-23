"""Invoice-only field-ID backfill with a separate durable journal and readback checks."""
import argparse
import contextlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from backfill_customer_branch import (BackfillError, Client, Store, branch_value,
    document_differences, dumps, find_field, load_map, omitted_lookup_candidate,
    run_lock, settings,
    validate_lookup_schema)
from repair_test_invoice import projection


PENDING_CONTINUATION_KEY = "pending_continuation:157368"
PENDING_CONTINUATION_MARKER = {
    "number": "157368",
    "document_id": "1462401000002738299",
    "branch_record_id": "1462401000020819564",
    "halt_reason": "Customer Branch field absent or duplicated; run discovery",
    "failure": "API rejected request (HTTP 401); branch remains unverified; halt retained",
}
PENDING_CONTINUATION_160059_KEY = "pending_continuation:160059"
PENDING_CONTINUATION_160059_MARKER = {
    "number": "160059",
    "document_id": "1462401000002906040",
    "branch_record_id": "1462401000020818804",
    "halt_reason": "Other business details changed; stopped for investigation",
    "failure": "Other business details changed; stopped for investigation",
}
PENDING_CONTINUATION_161528_KEY = "pending_continuation:161528"
PENDING_CONTINUATION_161528_MARKER = {
    "number": "161528",
    "document_id": "1462401000003848903",
    "branch_record_id": "1462401000020819564",
    "halt_reason": "Customer Branch update was not verified",
    "failure": "Customer Branch update was not verified",
}
PENDING_CONTINUATION_165534_KEY = "pending_continuation:165534"
PENDING_CONTINUATION_165534_MARKER = {
    "number": "165534",
    "document_id": "1462401000004191906",
    "branch_record_id": "1462401000020819279",
    "failure": "Invoice 165534 isolated; stock differences remain unverified",
}
PENDING_CONTINUATION_165818_KEY = "pending_continuation:165818"
PENDING_CONTINUATION_165818_MARKER = {
    "number": "165818",
    "document_id": "1462401000004205124",
    "branch_record_id": "1462401000020818724",
    "halt_reason": "Other business details changed; stopped for investigation",
    "failure": "Other business details changed; stopped for investigation",
}
PENDING_CONTINUATION_167764_KEY = "pending_continuation:167764"
PENDING_CONTINUATION_167764_MARKER = {
    "number": "167764",
    "document_id": "1462401000004345968",
    "branch_record_id": "1462401000020819279",
    "halt_reason": "Customer Branch update was not verified",
    "failure": "Customer Branch update was not verified",
}
PENDING_CONTINUATION_168436_KEY = "pending_continuation:168436"
PENDING_CONTINUATION_168436_MARKER = {
    "number": "168436",
    "document_id": "1462401000016155078",
    "branch_record_id": "1462401000020818784",
    "halt_reason": "Other business details changed; stopped for investigation",
    "failure": "Other business details changed; stopped for investigation",
}
PENDING_CONTINUATION_168777_KEY = "pending_continuation:168777"
PENDING_CONTINUATION_168777_MARKER = {
    "number": "168777",
    "document_id": "1462401000016175135",
    "branch_record_id": "1462401000020819039",
    "halt_reason": "Other business details changed; stopped for investigation",
    "failure": "Other business details changed; stopped for investigation",
}
PENDING_CONTINUATION_168812_KEY = "pending_continuation:168812"
PENDING_CONTINUATION_168812_MARKER = {
    "number": "168812",
    "document_id": "1462401000016177451",
    "branch_record_id": "1462401000020818924",
    "halt_reason": "Other business details changed; stopped for investigation",
    "failure": "Other business details changed; stopped for investigation",
}
PENDING_CONTINUATION_168905_KEY = "pending_continuation:168905"
PENDING_CONTINUATION_168905_MARKER = {
    "number": "168905",
    "document_id": "1462401000016182809",
    "branch_record_id": "1462401000020818924",
    "halt_reason": "Other business details changed; stopped for investigation",
    "failure": "Other business details changed; stopped for investigation",
}
STOCK_ONLY_RECONCILIATION = {
    "number": "160059",
    "document_id": "1462401000002906040",
    "branch_record_id": "1462401000020818804",
    "halt_reason": "Other business details changed; stopped for investigation",
    "failure": "Other business details changed; stopped for investigation",
}
STOCK_ONLY_DIFFERENCE_PATHS = {
    "/line_items/4/actual_available_for_sale_stock",
    "/line_items/4/actual_available_stock",
    "/line_items/4/available_for_sale_stock",
    "/line_items/4/available_stock",
    "/line_items/4/stock_on_hand",
}
INTERRUPTED_165534_STOCK_DIFFERENCE_PATHS = {
    "/line_items/0/actual_available_for_sale_stock",
    "/line_items/0/actual_available_stock",
    "/line_items/0/available_for_sale_stock",
    "/line_items/0/available_stock",
    "/line_items/0/stock_on_hand",
    "/line_items/1/actual_available_for_sale_stock",
    "/line_items/1/actual_available_stock",
    "/line_items/1/actual_committed_stock",
    "/line_items/1/available_for_sale_stock",
    "/line_items/1/available_stock",
    "/line_items/1/committed_stock",
    "/line_items/1/stock_on_hand",
}


def lookup(client, row, field_id):
    body = client.request("GET", "/invoices/" + row["zoho_id"] + "/customfields")
    doc = {"custom_fields": body.get("custom_fields")}
    field = find_field(doc, field_id)
    if field.get("api_name") != "cf_customer_branch" or field.get("data_type") != "lookup":
        raise BackfillError("Fresh invoice lookup endpoint has unexpected field metadata")
    return branch_value(doc, field_id), body


def branch_is_omitted(doc, field_id, schema):
    fields = doc.get("custom_fields")
    if not isinstance(fields, list) or any(not isinstance(field, dict) for field in fields):
        raise BackfillError("Document has invalid custom_fields")
    matches = [field for field in fields if str(field.get("customfield_id")) == field_id]
    if len(matches) > 1:
        raise BackfillError("Customer Branch field is duplicated")
    if matches:
        return False
    for container in (doc, doc.get("custom_field_hash", {})):
        if container.get(schema["api_name"] + "_unformatted") not in (None, ""):
            raise BackfillError("Unformatted branch mirror is populated")
    return omitted_lookup_candidate(doc, field_id, schema)


def verify_readback(client, row, schema, before, after, store=None,
                    evidence_kind="verification_lookup"):
    fid = schema["field_id"]
    changes = document_differences(projection(before, fid, schema),
                                   projection(after, fid, schema))
    if changes:
        if store is not None:
            store.record_evidence(row, "business_differences", changes)
        raise BackfillError("Other business details changed; stopped for investigation")
    if branch_is_omitted(after, fid, schema):
        if not branch_is_omitted(before, fid, schema):
            raise BackfillError("Post-write Customer Branch omitted when it was previously present")
        current, fields = lookup(client, row, fid)
        if store is not None:
            store.record_evidence(row, evidence_kind, fields)
        return current
    return branch_value(after, fid)


def complete_429_recovery(store, row, backup, remote_updates_performed):
    with store.db:
        store.db.execute("UPDATE documents SET status='updated_verified',error='' "
                         "WHERE type='invoice' AND zoho_id=?", (row["zoho_id"],))
        store.put("halted", None)
        store.put("http_429_recovery:" + str(time.time_ns()), {
            "number": row["number"], "document_id": row["zoho_id"], "backup": str(backup),
            "verified_by_fresh_read": True,
            "remote_updates_performed": remote_updates_performed})


def ensure_empty_branch(doc, field_id, schema):
    if branch_value(doc, field_id, schema) != "":
        raise BackfillError("Invoice branch is not empty")
    for container in (doc, doc.get("custom_field_hash", {})):
        if container.get("cf_customer_branch_unformatted") not in (None, ""):
            raise BackfillError("Unformatted branch mirror is populated")


def reconcile_omitted_field(client, store, schema, folder):
    halt = "Customer Branch field absent or duplicated; run discovery"
    if store.get("halted") != halt:
        raise BackfillError("Recovery is restricted to the saved omitted-field halt")
    failed = list(store.db.execute("SELECT * FROM documents WHERE status IN ('failed','updating')"))
    if len(failed) != 1 or failed[0]["status"] != "failed" or not failed[0]["original"]:
        raise BackfillError("Expected exactly one saved failed invoice with an original document")
    saved = failed[0]
    row, original = json.loads(saved["mapping"]), json.loads(saved["original"])
    if (row["type"] != "invoice" or row["number"] != "157368" or
            row["zoho_id"] != "1462401000002738299" or
            row["branch_record_id"] != "1462401000020819564" or saved["error"] != halt):
        raise BackfillError("Omitted-field recovery is restricted to the saved invoice 157368 attempt")

    evidence = list(store.db.execute("SELECT key,value FROM meta WHERE key LIKE ?",
        ("evidence:invoice:" + row["zoho_id"] + ":%",)))
    requests = [json.loads(item["value"]) for item in evidence if item["key"].endswith(":request")]
    errors = [json.loads(item["value"]) for item in evidence if item["key"].endswith(":write_error")]
    lookups = [json.loads(item["value"]) for item in evidence if item["key"].endswith(":lookup_before")]
    readbacks = [json.loads(item["value"]) for item in evidence if item["key"].endswith(":readback")]
    responses = [item for item in evidence if item["key"].endswith(":write_response")]
    recoveries = [item for item in evidence if ":omitted_recovery_" in item["key"]]
    expected = {"method": "PUT", "path": "/invoices/" + row["zoho_id"], "payload": {
        "custom_fields": [{"customfield_id": schema["field_id"], "value": row["branch_record_id"]}]}}
    if (requests != [expected] or len(errors) != 1 or len(lookups) != 1 or
            len(readbacks) != 1 or responses or recoveries or
            errors[0].get("error") != "Uncertain write: network failure; read before retry" or
            errors[0].get("http_status") is not None):
        raise BackfillError("Saved uncertain-write evidence is missing or ambiguous; halt retained")
    prior_readback = readbacks[0]
    if (not branch_is_omitted(original, schema["field_id"], schema) or
            not branch_is_omitted(prior_readback, schema["field_id"], schema) or
            branch_value(lookups[0], schema["field_id"]) != "" or
            projection(original, schema["field_id"], schema) !=
            projection(prior_readback, schema["field_id"], schema)):
        raise BackfillError("Saved omission evidence is inconsistent; halt retained")

    fresh = client.read(row)
    store.record_evidence(row, "omitted_recovery_fresh_readback", fresh)
    if projection(original, schema["field_id"], schema) != projection(fresh, schema["field_id"], schema):
        raise BackfillError("Invoice changed after the uncertain write; no retry sent")
    current, fields = lookup(client, row, schema["field_id"])
    store.record_evidence(row, "omitted_recovery_fresh_lookup", fields)
    if branch_is_omitted(fresh, schema["field_id"], schema):
        if not branch_is_omitted(original, schema["field_id"], schema):
            raise BackfillError("Invoice response omission is not consistent with its original")
    elif branch_value(fresh, schema["field_id"]) != current:
        raise BackfillError("Invoice GET and custom-fields endpoint disagree; halt retained")
    if current and current != row["branch_record_id"]:
        raise BackfillError("Customer Branch conflicts with the approved mapping; halt retained")
    if not current:
        ensure_empty_branch(fresh, schema["field_id"], schema)

    backup = folder / ("before_omitted_field_recovery_" + str(time.time_ns()) + ".sqlite3")
    with contextlib.closing(sqlite3.connect(backup)) as destination:
        store.db.backup(destination)
    if current == row["branch_record_id"]:
        with store.db:
            store.db.execute("UPDATE documents SET status='updated_verified',error='' "
                             "WHERE type='invoice' AND zoho_id=?", (row["zoho_id"],))
            store.put("halted", None)
            store.put("omitted_field_recovery:" + str(time.time_ns()), {
                "number": row["number"], "document_id": row["zoho_id"], "backup": str(backup),
                "verified_by": "dedicated_custom_fields_endpoint",
                "remote_updates_performed": 0})
        result = {"recovered_invoice": row["number"], "verified": True,
                  "remote_updates_performed": 0, "invoice_run_halt_cleared": True}
    else:
        payload = expected["payload"]
        store.update(row, "updating")
        store.record_evidence(row, "omitted_recovery_request", {
            "method": "PUT", "path": expected["path"], "payload": payload})
        try:
            try:
                response = client.request("PUT", expected["path"], payload)
                store.record_evidence(row, "omitted_recovery_write_response", response)
            except BackfillError as error:
                store.record_evidence(row, "omitted_recovery_write_error", {
                    "error": str(error), "http_status": getattr(error, "status", None),
                    "response": getattr(error, "body", None)})
            after = client.read(row)
            store.record_evidence(row, "omitted_recovery_readback", after)
            verified_branch = verify_readback(client, row, schema, fresh, after, store,
                                               "omitted_recovery_verification_lookup")
            if verified_branch != row["branch_record_id"]:
                raise BackfillError("Customer Branch update was not verified; halt retained")
        except BackfillError as error:
            store.update(row, "failed", str(error))
            raise
        with store.db:
            store.db.execute("UPDATE documents SET status='updated_verified',error='' "
                             "WHERE type='invoice' AND zoho_id=?", (row["zoho_id"],))
            store.put("halted", None)
            store.put("omitted_field_recovery:" + str(time.time_ns()), {
                "number": row["number"], "document_id": row["zoho_id"], "backup": str(backup),
                "verified_by": "invoice_readback_and_custom_fields_endpoint_if_omitted",
                "remote_updates_performed": 1})
        result = {"recovered_invoice": row["number"], "verified": True,
                  "remote_updates_performed": 1, "invoice_run_halt_cleared": True}
    print(dumps(result), flush=True)


def retry_inactive_template_rejection(client, store, schema, folder, template_active_confirmed):
    halt = "Customer Branch field absent or duplicated; run discovery"
    if not template_active_confirmed:
        raise BackfillError("Confirm the invoice template is active before this one-time retry")
    if store.get("halted") != halt:
        raise BackfillError("Recovery is restricted to the saved omitted-field halt")
    failed = list(store.db.execute("SELECT * FROM documents WHERE status IN ('failed','updating')"))
    if len(failed) != 1 or failed[0]["status"] != "failed" or not failed[0]["original"]:
        raise BackfillError("Expected exactly one saved failed invoice with an original document")
    saved = failed[0]
    row, original = json.loads(saved["mapping"]), json.loads(saved["original"])
    if (row["type"] != "invoice" or row["number"] != "157368" or
            row["zoho_id"] != "1462401000002738299" or
            row["branch_record_id"] != "1462401000020819564" or
            saved["error"] != "Customer Branch update was not verified; halt retained" or
            str(original.get("template_id")) != "1462401000015911003" or
            original.get("template_name") != "HQ Template"):
        raise BackfillError("Retry is restricted to the saved 157368 inactive-template rejection")

    evidence = {}
    for item in store.db.execute("SELECT key,value FROM meta WHERE key LIKE ?",
            ("evidence:invoice:" + row["zoho_id"] + ":%",)):
        evidence.setdefault(item["key"].rsplit(":", 1)[-1], []).append(json.loads(item["value"]))
    expected = {"method": "PUT", "path": "/invoices/" + row["zoho_id"], "payload": {
        "custom_fields": [{"customfield_id": schema["field_id"], "value": row["branch_record_id"]}]}}
    inactive_template_error = {
        "error": "API rejected request (HTTP 404)",
        "http_status": 404,
        "response": {"code": 1002, "message": "Inactive template cannot be used."}}
    if (evidence.get("request") != [expected] or
            len(evidence.get("write_error", [])) != 1 or
            evidence["write_error"][0].get("error") !=
                "Uncertain write: network failure; read before retry" or
            evidence["write_error"][0].get("http_status") is not None or
            len(evidence.get("lookup_before", [])) != 1 or
            evidence.get("readback") is None or len(evidence["readback"]) != 1 or
            evidence.get("write_response") or
            evidence.get("omitted_recovery_request") != [expected] or
            evidence.get("omitted_recovery_write_error") != [inactive_template_error] or
            len(evidence.get("omitted_recovery_fresh_readback", [])) != 1 or
            len(evidence.get("omitted_recovery_fresh_lookup", [])) != 1 or
            len(evidence.get("omitted_recovery_readback", [])) != 1 or
            len(evidence.get("omitted_recovery_verification_lookup", [])) != 1 or
            evidence.get("business_differences") or
            any("template_retry_" in item["key"] for item in
                store.db.execute("SELECT key FROM meta WHERE key LIKE ?",
                    ("evidence:invoice:" + row["zoho_id"] + ":%",))) or
            store.db.execute("SELECT 1 FROM meta WHERE key LIKE 'template_rejection_retry:%'").fetchone()):
        raise BackfillError("Saved inactive-template rejection evidence is missing or ambiguous; halt retained")

    saved_readbacks = [
        evidence["readback"][0],
        evidence["omitted_recovery_fresh_readback"][0],
        evidence["omitted_recovery_readback"][0]]
    saved_lookups = [
        evidence["lookup_before"][0],
        evidence["omitted_recovery_fresh_lookup"][0],
        evidence["omitted_recovery_verification_lookup"][0]]
    if (not branch_is_omitted(original, schema["field_id"], schema) or
            str(original.get("template_id")) != "1462401000015911003" or
            any(not branch_is_omitted(doc, schema["field_id"], schema) or
                projection(original, schema["field_id"], schema) !=
                projection(doc, schema["field_id"], schema) for doc in saved_readbacks) or
            any(branch_value(doc, schema["field_id"]) != "" for doc in saved_lookups)):
        raise BackfillError("Saved invoice or branch evidence changed; halt retained")

    fresh = client.read(row)
    store.record_evidence(row, "template_retry_fresh_readback", fresh)
    if projection(original, schema["field_id"], schema) != projection(fresh, schema["field_id"], schema):
        raise BackfillError("Invoice changed after the rejected write; no retry sent")
    current, fields = lookup(client, row, schema["field_id"])
    store.record_evidence(row, "template_retry_fresh_lookup", fields)
    if branch_is_omitted(fresh, schema["field_id"], schema):
        if not branch_is_omitted(original, schema["field_id"], schema):
            raise BackfillError("Invoice response omission is not consistent with its original")
    elif branch_value(fresh, schema["field_id"]) != current:
        raise BackfillError("Invoice GET and custom-fields endpoint disagree; halt retained")
    if current and current != row["branch_record_id"]:
        raise BackfillError("Customer Branch conflicts with the approved mapping; halt retained")
    if not current:
        ensure_empty_branch(fresh, schema["field_id"], schema)

    backup = folder / ("before_template_retry_" + str(time.time_ns()) + ".sqlite3")
    with contextlib.closing(sqlite3.connect(backup)) as destination:
        store.db.backup(destination)
    if current == row["branch_record_id"]:
        updates = 0
    else:
        store.update(row, "updating")
        store.record_evidence(row, "template_retry_request", expected)
        retry_error = None
        try:
            response = client.request("PUT", expected["path"], expected["payload"])
            store.record_evidence(row, "template_retry_write_response", response)
        except BackfillError as error:
            retry_error = error
            store.record_evidence(row, "template_retry_write_error", {
                "error": str(error), "http_status": getattr(error, "status", None),
                "response": getattr(error, "body", None)})
        try:
            after = client.read(row)
            store.record_evidence(row, "template_retry_readback", after)
            verified_branch = verify_readback(client, row, schema, fresh, after, store,
                                               "template_retry_verification_lookup")
            if verified_branch != row["branch_record_id"]:
                message = "Customer Branch update was not verified; halt retained"
                if retry_error is not None:
                    message = str(retry_error) + "; branch remains unverified; halt retained"
                raise BackfillError(message)
        except BackfillError as error:
            store.update(row, "failed", str(error))
            raise
        updates = 1

    with store.db:
        store.db.execute("UPDATE documents SET status='updated_verified',error='' "
                         "WHERE type='invoice' AND zoho_id=?", (row["zoho_id"],))
        store.put("halted", None)
        store.put("template_rejection_retry:" + str(time.time_ns()), {
            "number": row["number"], "document_id": row["zoho_id"], "backup": str(backup),
            "verified_by": "invoice_readback_and_custom_fields_endpoint_if_omitted",
            "remote_updates_performed": updates})
    print(dumps({"recovered_invoice": row["number"], "verified": True,
                 "remote_updates_performed": updates, "invoice_run_halt_cleared": True}), flush=True)


def retry_dedicated_customfield_endpoint(client, store, schema, folder):
    halt = "Customer Branch field absent or duplicated; run discovery"
    if store.get("halted") != halt:
        raise BackfillError("Recovery is restricted to the saved omitted-field halt")
    failed = list(store.db.execute("SELECT * FROM documents WHERE status IN ('failed','updating')"))
    if len(failed) != 1 or failed[0]["status"] != "failed" or not failed[0]["original"]:
        raise BackfillError("Expected exactly one saved failed invoice with an original document")
    saved = failed[0]
    row, original = json.loads(saved["mapping"]), json.loads(saved["original"])
    if (row["type"] != "invoice" or row["number"] != "157368" or
            row["zoho_id"] != "1462401000002738299" or
            row["branch_record_id"] != "1462401000020819564" or
            saved["error"] != "API rejected request (HTTP 404); branch remains unverified; halt retained"):
        raise BackfillError("Dedicated endpoint retry is restricted to the saved 157368 failure")

    evidence = {}
    for item in store.db.execute("SELECT key,value FROM meta WHERE key LIKE ?",
            ("evidence:invoice:" + row["zoho_id"] + ":%",)):
        evidence.setdefault(item["key"].rsplit(":", 1)[-1], []).append(json.loads(item["value"]))
    expected_invoice = {"method": "PUT", "path": "/invoices/" + row["zoho_id"], "payload": {
        "custom_fields": [{"customfield_id": schema["field_id"], "value": row["branch_record_id"]}]}}
    expected_dedicated = {"method": "PUT", "path": "/invoice/" + row["zoho_id"] + "/customfields",
                          "payload": expected_invoice["payload"]}
    first_rejection = {"error": "API rejected request (HTTP 404)", "http_status": 404,
                       "response": {"code": 1002, "message": "Inactive template cannot be used."}}
    if (evidence.get("request") != [expected_invoice] or
            evidence.get("write_error") != [{"error": "Uncertain write: network failure; read before retry",
                                             "http_status": None, "response": None}] or
            evidence.get("lookup_before") is None or len(evidence["lookup_before"]) != 1 or
            evidence.get("readback") is None or len(evidence["readback"]) != 1 or
            evidence.get("omitted_recovery_request") != [expected_invoice] or
            evidence.get("omitted_recovery_write_error") != [first_rejection] or
            evidence.get("template_retry_request") != [expected_invoice] or
            evidence.get("template_retry_write_error") != [first_rejection] or
            evidence.get("write_response") or evidence.get("omitted_recovery_write_response") or
            evidence.get("template_retry_write_response") or evidence.get("business_differences") or
            any("customfield_endpoint_retry" in item["key"] for item in
                store.db.execute("SELECT key FROM meta WHERE key LIKE ?",
                    ("evidence:invoice:" + row["zoho_id"] + ":%",))) or
            store.db.execute("SELECT 1 FROM meta WHERE key LIKE 'dedicated_customfield_retry:%'").fetchone()):
        raise BackfillError("Saved rejected-write history is missing or ambiguous; halt retained")

    readback_kinds = ("readback", "omitted_recovery_fresh_readback", "omitted_recovery_readback",
                      "template_retry_fresh_readback", "template_retry_readback")
    lookup_kinds = ("lookup_before", "omitted_recovery_fresh_lookup",
                    "omitted_recovery_verification_lookup", "template_retry_fresh_lookup",
                    "template_retry_verification_lookup")
    if (any(len(evidence.get(kind, [])) != 1 for kind in readback_kinds + lookup_kinds) or
            not branch_is_omitted(original, schema["field_id"], schema) or
            any(not branch_is_omitted(evidence[kind][0], schema["field_id"], schema)
                or projection(original, schema["field_id"], schema) !=
                projection(evidence[kind][0], schema["field_id"], schema)
                for kind in readback_kinds) or
            any(branch_value(evidence[kind][0], schema["field_id"]) != "" for kind in lookup_kinds)):
        raise BackfillError("Saved invoice or branch evidence changed; halt retained")

    fresh = client.read(row)
    store.record_evidence(row, "customfield_endpoint_retry_fresh_readback", fresh)
    if projection(original, schema["field_id"], schema) != projection(fresh, schema["field_id"], schema):
        raise BackfillError("Invoice changed after the rejected writes; no retry sent")
    current, fields = lookup(client, row, schema["field_id"])
    store.record_evidence(row, "customfield_endpoint_retry_fresh_lookup", fields)
    if branch_is_omitted(fresh, schema["field_id"], schema):
        if not branch_is_omitted(original, schema["field_id"], schema):
            raise BackfillError("Invoice response omission is not consistent with its original")
    elif branch_value(fresh, schema["field_id"]) != current:
        raise BackfillError("Invoice GET and custom-fields endpoint disagree; halt retained")
    if current and current != row["branch_record_id"]:
        raise BackfillError("Customer Branch conflicts with the approved mapping; halt retained")
    if not current:
        ensure_empty_branch(fresh, schema["field_id"], schema)

    backup = folder / ("before_dedicated_customfield_retry_" + str(time.time_ns()) + ".sqlite3")
    with contextlib.closing(sqlite3.connect(backup)) as destination:
        store.db.backup(destination)
    updates = 0
    if current != row["branch_record_id"]:
        store.update(row, "updating")
        store.record_evidence(row, "customfield_endpoint_retry_request", expected_dedicated)
        retry_error = None
        try:
            response = client.request("PUT", expected_dedicated["path"], expected_dedicated["payload"])
            store.record_evidence(row, "customfield_endpoint_retry_write_response", response)
        except BackfillError as error:
            retry_error = error
            store.record_evidence(row, "customfield_endpoint_retry_write_error", {
                "error": str(error), "http_status": getattr(error, "status", None),
                "response": getattr(error, "body", None)})
        try:
            after = client.read(row)
            store.record_evidence(row, "customfield_endpoint_retry_readback", after)
            verified_branch = verify_readback(client, row, schema, fresh, after, store,
                                               "customfield_endpoint_retry_verification_lookup")
            if verified_branch != row["branch_record_id"]:
                message = "Customer Branch update was not verified; halt retained"
                if retry_error is not None:
                    message = str(retry_error) + "; branch remains unverified; halt retained"
                raise BackfillError(message)
        except BackfillError as error:
            store.update(row, "failed", str(error))
            raise
        updates = 1

    with store.db:
        store.db.execute("UPDATE documents SET status='updated_verified',error='' "
                         "WHERE type='invoice' AND zoho_id=?", (row["zoho_id"],))
        store.put("halted", None)
        store.put("dedicated_customfield_retry:" + str(time.time_ns()), {
            "number": row["number"], "document_id": row["zoho_id"], "backup": str(backup),
            "endpoint": expected_dedicated["path"],
            "verified_by": "invoice_readback_and_custom_fields_endpoint_if_omitted",
            "remote_updates_performed": updates})
    print(dumps({"recovered_invoice": row["number"], "verified": True,
                 "remote_updates_performed": updates, "invoice_run_halt_cleared": True}), flush=True)


def reconcile_manual_branch_update(client, store, schema, folder):
    halt = "Customer Branch field absent or duplicated; run discovery"
    continuation_marker = store.get(PENDING_CONTINUATION_KEY)
    if continuation_marker is not None:
        if continuation_marker != PENDING_CONTINUATION_MARKER or store.get("halted") is not None:
            raise BackfillError("Manual verification requires the saved 157368 marker and no active halt")
        prepare_pending_continuation(store)
        saved = store.db.execute(
            "SELECT * FROM documents WHERE type='invoice' AND zoho_id=?",
            (PENDING_CONTINUATION_MARKER["document_id"],)).fetchone()
        if saved is None or saved["status"] != "failed" or not saved["original"]:
            raise BackfillError("Saved invoice 157368 failure is missing; halt retained")
    else:
        if store.get("halted") != halt:
            raise BackfillError("Recovery is restricted to the saved omitted-field halt")
        failed = list(store.db.execute("SELECT * FROM documents WHERE status IN ('failed','updating')"))
        if len(failed) != 1 or failed[0]["status"] != "failed" or not failed[0]["original"]:
            raise BackfillError("Expected exactly one saved failed invoice with an original document")
        saved = failed[0]
    row, original = json.loads(saved["mapping"]), json.loads(saved["original"])
    if (row["type"] != "invoice" or row["number"] != "157368" or
            row["zoho_id"] != "1462401000002738299" or
            row["branch_record_id"] != "1462401000020819564" or
            saved["error"] != "API rejected request (HTTP 401); branch remains unverified; halt retained"):
        raise BackfillError("Manual verification is restricted to the saved 157368 authorization failure")

    evidence = {}
    for item in store.db.execute("SELECT key,value FROM meta WHERE key LIKE ?",
            ("evidence:invoice:" + row["zoho_id"] + ":%",)):
        evidence.setdefault(item["key"].rsplit(":", 1)[-1], []).append(json.loads(item["value"]))
    expected_invoice = {"method": "PUT", "path": "/invoices/" + row["zoho_id"], "payload": {
        "custom_fields": [{"customfield_id": schema["field_id"], "value": row["branch_record_id"]}]}}
    expected_dedicated = {"method": "PUT", "path": "/invoice/" + row["zoho_id"] + "/customfields",
                          "payload": expected_invoice["payload"]}
    rejected = {"error": "API rejected request (HTTP 404)", "http_status": 404,
                "response": {"code": 1002, "message": "Inactive template cannot be used."}}
    unauthorized = {"error": "API rejected request (HTTP 401)", "http_status": 401,
                    "response": {"code": 57, "message": "You are not authorized to perform this operation"}}
    request_kinds = ("request", "omitted_recovery_request", "template_retry_request")
    if (any(evidence.get(kind) != [expected_invoice] for kind in request_kinds) or
            evidence.get("customfield_endpoint_retry_request") != [expected_dedicated] or
            evidence.get("write_error") != [{"error": "Uncertain write: network failure; read before retry",
                                             "http_status": None, "response": None}] or
            evidence.get("omitted_recovery_write_error") != [rejected] or
            evidence.get("template_retry_write_error") != [rejected] or
            evidence.get("customfield_endpoint_retry_write_error") != [unauthorized] or
            any(evidence.get(kind) for kind in
                ("write_response", "omitted_recovery_write_response",
                 "template_retry_write_response", "customfield_endpoint_retry_write_response",
                 "business_differences"))):
        raise BackfillError("Saved authorization-failure history is missing or ambiguous; halt retained")

    readback_kinds = (
        "readback", "omitted_recovery_fresh_readback", "omitted_recovery_readback",
        "template_retry_fresh_readback", "template_retry_readback",
        "customfield_endpoint_retry_fresh_readback", "customfield_endpoint_retry_readback")
    lookup_kinds = (
        "lookup_before", "omitted_recovery_fresh_lookup",
        "omitted_recovery_verification_lookup", "template_retry_fresh_lookup",
        "template_retry_verification_lookup", "customfield_endpoint_retry_fresh_lookup",
        "customfield_endpoint_retry_verification_lookup")
    if (any(len(evidence.get(kind, [])) != 1 for kind in readback_kinds + lookup_kinds) or
            not branch_is_omitted(original, schema["field_id"], schema) or
            any(not branch_is_omitted(evidence[kind][0], schema["field_id"], schema) or
                projection(original, schema["field_id"], schema) !=
                projection(evidence[kind][0], schema["field_id"], schema)
                for kind in readback_kinds) or
            any(branch_value(evidence[kind][0], schema["field_id"]) != "" for kind in lookup_kinds)):
        raise BackfillError("Saved invoice or branch evidence changed; halt retained")

    fresh = client.read(row)
    store.record_evidence(row, "manual_branch_reconciliation_readback", fresh)
    if projection(original, schema["field_id"], schema) != projection(fresh, schema["field_id"], schema):
        raise BackfillError("Invoice changed since the saved snapshot; halt retained")
    current, fields = lookup(client, row, schema["field_id"])
    store.record_evidence(row, "manual_branch_reconciliation_lookup", fields)
    if branch_is_omitted(fresh, schema["field_id"], schema):
        if not branch_is_omitted(original, schema["field_id"], schema):
            raise BackfillError("Invoice response omission is not consistent with its original")
    elif branch_value(fresh, schema["field_id"]) != current:
        raise BackfillError("Invoice GET and custom-fields endpoint disagree; halt retained")
    if current != row["branch_record_id"]:
        raise BackfillError("Expected branch is not saved in Zoho; halt retained")

    backup = folder / ("before_manual_branch_reconciliation_" + str(time.time_ns()) + ".sqlite3")
    with contextlib.closing(sqlite3.connect(backup)) as destination:
        store.db.backup(destination)
    with store.db:
        store.db.execute("UPDATE documents SET status='updated_verified',error='' "
                         "WHERE type='invoice' AND zoho_id=?", (row["zoho_id"],))
        store.put("halted", None)
        store.put("manual_branch_reconciliation:" + str(time.time_ns()), {
            "number": row["number"], "document_id": row["zoho_id"], "backup": str(backup),
            "verified_by": "fresh_custom_fields_endpoint",
            "remote_updates_performed": 0})
    print(dumps({"recovered_invoice": row["number"], "verified": True,
                 "remote_updates_performed": 0, "invoice_run_halt_cleared": True}), flush=True)


def update_one(client, store, row, schema, test=False, recovery_backup=None,
               before_snapshot=None, lookup_snapshot=None):
    fid = schema["field_id"]
    saved = store.db.execute("SELECT * FROM documents WHERE type=? AND zoho_id=?",
                             (row["type"], row["zoho_id"])).fetchone()
    if saved["original"] and recovery_backup is None:
        # Never automatically resend an attempted write, even after process termination.
        raise BackfillError("Saved attempt requires reconciliation before retry")
    if recovery_backup is not None and (
            store.get("halted") != "API rejected request (HTTP 429)" or
            saved["status"] != "failed" or not saved["original"] or
            before_snapshot is None or lookup_snapshot is None):
        raise BackfillError("429 retry is not authorized by the saved failed attempt")
    if recovery_backup is None:
        before = client.read(row)
        current, fields = lookup(client, row, fid)
    else:
        before = before_snapshot
        current, fields = lookup_snapshot
    if recovery_backup is not None:
        original = json.loads(saved["original"])
        if projection(original, fid, schema) != projection(before, fid, schema):
            raise BackfillError("Invoice changed after 429 reconciliation; retry not sent")
    present = [f for f in before.get("custom_fields", []) if str(f.get("customfield_id")) == fid]
    if present and branch_value(before, fid) != current:
        raise BackfillError("Invoice GET and custom-fields endpoint disagree")
    if current:
        if current == row["branch_record_id"]:
            if recovery_backup is None:
                store.update(row, "already_correct")
            else:
                branch_value(before, fid)
                complete_429_recovery(store, row, recovery_backup, 0)
        elif recovery_backup is not None:
            raise BackfillError("Customer Branch changed after 429 reconciliation; retry not sent")
        else:
            store.update(row, "conflict")
        return False
    ensure_empty_branch(before, fid, schema)
    # Save evidence transactionally before the only write.
    store.update(row, "updating", original=before)
    store.record_evidence(row, "lookup_before", fields)
    payload = {"custom_fields": [{"customfield_id": fid, "value": row["branch_record_id"]}]}
    store.record_evidence(row, "request", {"method": "PUT", "path": "/invoices/" + row["zoho_id"],
                                           "payload": payload})
    try:
        response = client.request("PUT", "/invoices/" + row["zoho_id"], payload)
        store.record_evidence(row, "write_response", response)
    except BackfillError as error:
        store.record_evidence(row, "write_error", {"error": str(error),
            "http_status": getattr(error, "status", None), "response": getattr(error, "body", None)})
    after = client.read(row)
    store.record_evidence(row, "readback", after)
    verified_branch = verify_readback(client, row, schema, before, after, store)
    if verified_branch != row["branch_record_id"]:
        raise BackfillError("Customer Branch update was not verified")
    if recovery_backup is None:
        store.update(row, "updated_verified", test=test)
    else:
        complete_429_recovery(store, row, recovery_backup, 1)
    return True


def reconcile_429(client, store, schema, folder):
    if store.get("halted") != "API rejected request (HTTP 429)":
        raise BackfillError("Recovery is restricted to the saved HTTP 429 halt")
    failed = recovery_candidates(store)
    if len(failed) != 1 or failed[0]["status"] != "failed" or not failed[0]["original"]:
        raise BackfillError("Expected exactly one saved failed invoice with an original document")
    saved = failed[0]
    row, before = json.loads(saved["mapping"]), json.loads(saved["original"])
    if row["type"] != "invoice" or row["number"] != "156416":
        raise BackfillError("HTTP 429 recovery is restricted to invoice 156416")
    if branch_value(before, schema["field_id"], schema) != "":
        raise BackfillError("Saved original does not show a blank Customer Branch")
    evidence = list(store.db.execute("SELECT key,value FROM meta WHERE key LIKE ?",
                                     ("evidence:invoice:" + row["zoho_id"] + ":%",)))
    requests = [json.loads(item["value"]) for item in evidence if item["key"].endswith(":request")]
    errors = [json.loads(item["value"]) for item in evidence if item["key"].endswith(":write_error")]
    lookups = [json.loads(item["value"]) for item in evidence if item["key"].endswith(":lookup_before")]
    responses = [item for item in evidence if item["key"].endswith(":write_response")]
    readbacks = [item for item in evidence if item["key"].endswith((":readback", ":recovery_readback"))]
    expected = {"method": "PUT", "path": "/invoices/" + row["zoho_id"], "payload": {
        "custom_fields": [{"customfield_id": schema["field_id"], "value": row["branch_record_id"]}]}}
    if (requests != [expected] or len(errors) != 1 or len(lookups) != 1 or responses or readbacks or
            saved["error"] != "API rejected request (HTTP 429)" or
            "Uncertain write: temporary API failure" not in errors[0].get("error", "") or
            errors[0].get("http_status") not in (None, 429)):
        raise BackfillError("Saved 429 write evidence is missing or ambiguous; halt retained")
    if branch_value(lookups[0], schema["field_id"]) != "":
        raise BackfillError("Saved lookup evidence does not show a blank Customer Branch")

    backup = folder / ("before_429_recovery_" + str(time.time_ns()) + ".sqlite3")
    with contextlib.closing(sqlite3.connect(backup)) as destination:
        store.db.backup(destination)
    after = client.read(row)
    store.record_evidence(row, "http_429_recovery_readback", after)
    differences = document_differences(
        projection(before, schema["field_id"], schema),
        projection(after, schema["field_id"], schema))
    if differences:
        store.record_evidence(row, "http_429_business_differences", differences)
        raise BackfillError("Recovery read shows business differences; halt retained and no retry sent")

    current, fields = lookup(client, row, schema["field_id"])
    store.record_evidence(row, "http_429_recovery_lookup", fields)
    present = [f for f in after.get("custom_fields", [])
               if str(f.get("customfield_id")) == schema["field_id"]]
    if present and branch_value(after, schema["field_id"]) != current:
        raise BackfillError("Invoice GET and custom-fields endpoint disagree; halt retained")
    if not present and branch_is_omitted(after, schema["field_id"], schema):
        if not branch_is_omitted(before, schema["field_id"], schema):
            raise BackfillError("Invoice response omission is not consistent with its original")
    if current and current != row["branch_record_id"]:
        raise BackfillError("Customer Branch conflicts with the approved mapping; halt retained")

    if current == row["branch_record_id"]:
        complete_429_recovery(store, row, backup, 0)
        result = {"recovered_invoice": row["number"], "verified": True,
                  "remote_updates_performed": 0, "invoice_run_halt_cleared": True}
    else:
        try:
            changed = update_one(client, store, row, schema, recovery_backup=backup,
                                 before_snapshot=after, lookup_snapshot=(current, fields))
        except BackfillError as error:
            store.update(row, "failed", str(error))
            raise
        result = {"recovered_invoice": row["number"], "verified": True,
                  "remote_updates_performed": int(changed),
                  "invoice_run_halt_cleared": store.get("halted") is None}
    print(dumps(result), flush=True)


def reconcile_read_failure(client, store, schema, folder):
    halt = "Read failed after bounded retries"
    if store.get("halted") != halt:
        raise BackfillError("Recovery is restricted to the investigated read-retry halt")
    failed = recovery_candidates(store)
    if (len(failed) != 1 or failed[0]["status"] != "failed" or
            failed[0]["error"] != halt or not failed[0]["original"]):
        raise BackfillError("Expected exactly one saved attempted invoice")
    saved = failed[0]
    row, before = json.loads(saved["mapping"]), json.loads(saved["original"])
    evidence = list(store.db.execute("SELECT key,value FROM meta WHERE key LIKE ?",
                                    ("evidence:invoice:" + row["zoho_id"] + ":%",)))
    responses = [json.loads(item["value"]) for item in evidence if item["key"].endswith(":write_response")]
    requests = [json.loads(item["value"]) for item in evidence if item["key"].endswith(":request")]
    write_errors = [json.loads(item["value"]) for item in evidence
                    if item["key"].endswith(":write_error")]
    expected = {"method": "PUT", "path": "/invoices/" + row["zoho_id"], "payload": {
        "custom_fields": [{"customfield_id": schema["field_id"], "value": row["branch_record_id"]}]}}
    if requests != [expected]:
        raise BackfillError("Saved request evidence is missing or ambiguous")
    if responses:
        if (len(responses) != 1 or responses[0].get("code") != 0 or write_errors):
            raise BackfillError("Saved successful write evidence is missing or ambiguous")
    else:
        evidence_kinds = {item["key"].rsplit(":", 1)[-1] for item in evidence}
        expected_error = {
            "error": "Uncertain write: network failure; read before retry",
            "http_status": None,
            "response": None,
        }
        lookups = [json.loads(item["value"]) for item in evidence
                   if item["key"].endswith(":lookup_before")]
        original_blank = (branch_is_omitted(before, schema["field_id"], schema) or
                          branch_value(before, schema["field_id"]) == "")
        if (evidence_kinds != {"lookup_before", "request", "write_error"} or
                write_errors != [expected_error] or len(lookups) != 1 or
                branch_value(lookups[0], schema["field_id"]) != "" or not original_blank):
            raise BackfillError("Saved uncertain-write evidence is missing or ambiguous")
    after = client.read(row)
    store.record_evidence(row, "recovery_readback", after)
    verified_branch = verify_readback(client, row, schema, before, after, store,
                                      "recovery_verification_lookup")
    if verified_branch != row["branch_record_id"]:
        raise BackfillError("Recovery read does not verify; halt retained and no write sent")
    backup = folder / ("before_recovery_" + str(time.time_ns()) + ".sqlite3")
    with contextlib.closing(sqlite3.connect(backup)) as destination:
        store.db.backup(destination)
    with store.db:
        store.db.execute("UPDATE documents SET status='updated_verified',error='' WHERE type='invoice' AND zoho_id=?",
                         (row["zoho_id"],))
        store.put("halted", None)
        store.put("read_failure_recovery:" + str(time.time_ns()), {
            "number": row["number"], "document_id": row["zoho_id"], "backup": str(backup),
            "verified_by_fresh_read": True, "remote_updates_performed": 0})
    print(dumps({"recovered_invoice": row["number"], "verified": True,
                 "remote_updates_performed": 0, "invoice_run_halt_cleared": True}), flush=True)


def reconcile_stock_only_difference(client, store, schema, folder):
    row_id = STOCK_ONLY_RECONCILIATION["document_id"]
    if schema["field_id"] != "1462401000020819910":
        raise BackfillError("Stock-only reconciliation field differs from the approved invoice field")
    if store.get(PENDING_CONTINUATION_KEY) != PENDING_CONTINUATION_MARKER:
        raise BackfillError("Stock-only reconciliation requires the saved 157368 continuation marker")
    if store.get("halted") != STOCK_ONLY_RECONCILIATION["halt_reason"]:
        raise BackfillError("Recovery is restricted to the saved stock-availability halt")

    failed = list(store.db.execute(
        "SELECT * FROM documents WHERE status='failed' ORDER BY zoho_id"))
    if len(failed) != 2:
        raise BackfillError("Expected only the isolated 157368 and failed invoice 160059")
    saved = next((item for item in failed if item["type"] == "invoice" and
                  item["zoho_id"] == row_id), None)
    isolated = next((item for item in failed if item["type"] == "invoice" and
                     item["zoho_id"] == PENDING_CONTINUATION_MARKER["document_id"]), None)
    if (saved is None or isolated is None or
            saved["error"] != STOCK_ONLY_RECONCILIATION["failure"] or
            isolated["error"] != PENDING_CONTINUATION_MARKER["failure"] or
            not saved["original"]):
        raise BackfillError("Saved invoice failures do not match the two reviewed rows")

    row = json.loads(saved["mapping"])
    original = json.loads(saved["original"])
    if (row["type"] != "invoice" or row["number"] != STOCK_ONLY_RECONCILIATION["number"] or
            row["branch_record_id"] != STOCK_ONLY_RECONCILIATION["branch_record_id"]):
        raise BackfillError("Stock-only reconciliation is restricted to invoice 160059")

    evidence = {}
    for item in store.db.execute("SELECT key,value FROM meta WHERE key LIKE ?",
            ("evidence:invoice:" + row_id + ":%",)):
        evidence.setdefault(item["key"].rsplit(":", 1)[-1], []).append(json.loads(item["value"]))
    expected_request = {"method": "PUT", "path": "/invoices/" + row_id, "payload": {
        "custom_fields": [{"customfield_id": schema["field_id"],
                           "value": row["branch_record_id"]}]}}
    requests = evidence.get("request", [])
    responses = evidence.get("write_response", [])
    write_errors = evidence.get("write_error", [])
    lookups = evidence.get("lookup_before", [])
    readbacks = evidence.get("readback", [])
    differences = evidence.get("business_differences", [])
    if (requests != [expected_request] or len(responses) != 1 or
            responses[0].get("code") != 0 or write_errors or len(lookups) != 1 or
            len(readbacks) != 1 or len(differences) != 1):
        raise BackfillError("Saved invoice 160059 write evidence is missing or ambiguous")
    if branch_value({"custom_fields": lookups[0].get("custom_fields")},
                    schema["field_id"]) != "":
        raise BackfillError("Saved invoice 160059 pre-write branch was not blank")
    saved_readback = readbacks[0]
    saved_value = branch_value(saved_readback, schema["field_id"])
    if saved_value != row["branch_record_id"]:
        raise BackfillError("Saved invoice 160059 readback does not contain its mapped branch")

    before_projection = projection(original, schema["field_id"], schema)
    saved_projection = projection(saved_readback, schema["field_id"], schema)
    saved_differences = document_differences(before_projection, saved_projection)
    saved_paths = {item["path"] for item in saved_differences}
    recorded_differences = differences[0]
    recorded_paths = {item.get("path") for item in recorded_differences
                      if isinstance(item, dict)}
    if (saved_paths != STOCK_ONLY_DIFFERENCE_PATHS or
            recorded_paths != STOCK_ONLY_DIFFERENCE_PATHS):
        raise BackfillError("Invoice 160059 differences exceed the five recorded stock-availability fields")

    backup = folder / ("before_stock_only_reconciliation_" + str(time.time_ns()) + ".sqlite3")
    with contextlib.closing(sqlite3.connect(backup)) as destination:
        store.db.backup(destination)

    fresh = client.read(row)
    store.record_evidence(row, "stock_only_reconciliation_readback", fresh)
    fresh_differences = document_differences(
        before_projection, projection(fresh, schema["field_id"], schema))
    fresh_paths = {item["path"] for item in fresh_differences}
    saved_to_fresh = document_differences(
        saved_projection, projection(fresh, schema["field_id"], schema))
    later_paths = {item["path"] for item in saved_to_fresh}
    if (not fresh_paths.issubset(STOCK_ONLY_DIFFERENCE_PATHS) or
            not later_paths.issubset(STOCK_ONLY_DIFFERENCE_PATHS)):
        store.record_evidence(row, "stock_only_reconciliation_differences",
                              {"from_original": fresh_differences,
                               "from_saved_readback": saved_to_fresh})
        raise BackfillError("Fresh invoice read has additional business differences; halt retained")

    current, fields = lookup(client, row, schema["field_id"])
    store.record_evidence(row, "stock_only_reconciliation_lookup", fields)
    if branch_is_omitted(fresh, schema["field_id"], schema):
        if not branch_is_omitted(saved_readback, schema["field_id"], schema):
            raise BackfillError("Fresh invoice field omission differs from saved readback")
    elif branch_value(fresh, schema["field_id"]) != current:
        raise BackfillError("Invoice GET and custom-fields endpoint disagree; halt retained")
    if current != row["branch_record_id"]:
        raise BackfillError("Fresh invoice 160059 branch is not the mapped branch; halt retained")

    with store.db:
        store.db.execute("UPDATE documents SET status='updated_verified',error='' "
                         "WHERE type='invoice' AND zoho_id=?", (row_id,))
        store.put("halted", None)
        store.put("stock_only_reconciliation:" + str(time.time_ns()), {
            "number": row["number"], "document_id": row_id, "backup": str(backup),
            "verified_by": "fresh_invoice_and_custom_fields_reads",
            "allowed_difference_paths": sorted(STOCK_ONLY_DIFFERENCE_PATHS),
            "remote_updates_performed": 0})
    print(dumps({"recovered_invoice": row["number"], "verified": True,
                 "remote_updates_performed": 0, "invoice_run_halt_cleared": True}), flush=True)


def reconcile_interrupted(client, store, schema):
    """Read-only reconciliation after Ctrl+C; never resend an uncertain request."""
    for saved in list(store.db.execute("SELECT * FROM documents WHERE status='updating'")):
        row, before = json.loads(saved["mapping"]), json.loads(saved["original"])
        after = client.read(row)
        store.record_evidence(row, "interruption_readback", after)
        value = verify_readback(client, row, schema, before, after, store,
                                "interruption_verification_lookup")
        if value == row["branch_record_id"]:
            store.update(row, "updated_verified")
        else:
            request = store.db.execute("SELECT 1 FROM meta WHERE key LIKE ?",
                ("evidence:invoice:" + row["zoho_id"] + ":%:request",)).fetchone()
            if value or request:
                raise BackfillError("Interrupted request is uncertain; no automatic resend permitted")
            # The request journal is always committed before the network call.
            store.record_evidence(row, "interruption_original", before)
            with store.db:
                store.db.execute("UPDATE documents SET status='pending',original=NULL WHERE type='invoice' AND zoho_id=?",
                                 (row["zoho_id"],))


def recovery_candidates(store):
    rows = list(store.db.execute("SELECT * FROM documents WHERE status IN ('failed','updating')"))
    if store.get(PENDING_CONTINUATION_KEY) == PENDING_CONTINUATION_MARKER:
        isolated_ids = {PENDING_CONTINUATION_MARKER["document_id"]}
        if store.get(PENDING_CONTINUATION_160059_KEY) == PENDING_CONTINUATION_160059_MARKER:
            isolated_ids.add(PENDING_CONTINUATION_160059_MARKER["document_id"])
        if store.get(PENDING_CONTINUATION_161528_KEY) == PENDING_CONTINUATION_161528_MARKER:
            isolated_ids.add(PENDING_CONTINUATION_161528_MARKER["document_id"])
        if store.get(PENDING_CONTINUATION_165534_KEY) == PENDING_CONTINUATION_165534_MARKER:
            isolated_ids.add(PENDING_CONTINUATION_165534_MARKER["document_id"])
        if store.get(PENDING_CONTINUATION_165818_KEY) == PENDING_CONTINUATION_165818_MARKER:
            isolated_ids.add(PENDING_CONTINUATION_165818_MARKER["document_id"])
        if store.get(PENDING_CONTINUATION_167764_KEY) == PENDING_CONTINUATION_167764_MARKER:
            isolated_ids.add(PENDING_CONTINUATION_167764_MARKER["document_id"])
        if store.get(PENDING_CONTINUATION_168436_KEY) == PENDING_CONTINUATION_168436_MARKER:
            isolated_ids.add(PENDING_CONTINUATION_168436_MARKER["document_id"])
        if store.get(PENDING_CONTINUATION_168777_KEY) == PENDING_CONTINUATION_168777_MARKER:
            isolated_ids.add(PENDING_CONTINUATION_168777_MARKER["document_id"])
        if store.get(PENDING_CONTINUATION_168812_KEY) == PENDING_CONTINUATION_168812_MARKER:
            isolated_ids.add(PENDING_CONTINUATION_168812_MARKER["document_id"])
        if store.get(PENDING_CONTINUATION_168905_KEY) == PENDING_CONTINUATION_168905_MARKER:
            isolated_ids.add(PENDING_CONTINUATION_168905_MARKER["document_id"])
        rows = [row for row in rows
                if not (row["type"] == "invoice" and
                        row["zoho_id"] in isolated_ids)]
    return rows


def validate_interrupted_165534_isolation(store, saved, already_isolated):
    marker = PENDING_CONTINUATION_165534_MARKER
    expected_status = "failed" if already_isolated else "updating"
    expected_error = marker["failure"] if already_isolated else ""
    mapping = json.loads(saved["mapping"])
    field_id = "1462401000020819910"
    schema = {"field_id": field_id, "api_name": "cf_customer_branch"}
    if (saved["type"] != "invoice" or saved["zoho_id"] != marker["document_id"] or
            saved["status"] != expected_status or saved["error"] != expected_error or
            not saved["original"] or mapping.get("number") != marker["number"] or
            mapping.get("branch_record_id") != marker["branch_record_id"]):
        raise BackfillError("Saved invoice 165534 interruption evidence changed; halt retained")

    original = json.loads(saved["original"])
    original_branch_blank = (branch_is_omitted(original, field_id, schema) or
                             branch_value(original, field_id) == "")
    if not original_branch_blank:
        raise BackfillError("Saved invoice 165534 original branch is not blank; halt retained")

    prefix = "evidence:invoice:" + marker["document_id"] + ":"
    evidence = {}
    for item in store.db.execute("SELECT key,value FROM meta WHERE key LIKE ?",
                                 (prefix + "%",)):
        evidence.setdefault(item["key"].rsplit(":", 1)[-1], []).append(
            json.loads(item["value"]))
    expected_kinds = {"lookup_before", "request", "interruption_readback",
                      "business_differences"}
    expected_request = {
        "method": "PUT", "path": "/invoices/" + marker["document_id"],
        "payload": {"custom_fields": [{
            "customfield_id": field_id, "value": marker["branch_record_id"]}]}}
    readbacks = evidence.get("interruption_readback", [])
    differences = evidence.get("business_differences", [])
    if (set(evidence) != expected_kinds or
            evidence.get("request") != [expected_request] or
            len(evidence.get("lookup_before", [])) != 1 or not readbacks or
            len(readbacks) != len(differences) or
            branch_value(evidence["lookup_before"][0], field_id) != "" or
            any(branch_value(readback, field_id) != marker["branch_record_id"]
                for readback in readbacks)):
        raise BackfillError("Saved invoice 165534 request/readback evidence is ambiguous; halt retained")
    for group in differences:
        if not isinstance(group, list):
            raise BackfillError("Invoice 165534 has invalid business-difference evidence; halt retained")
        paths = {item.get("path") for item in group if isinstance(item, dict)}
        if (len(group) != len(paths) or paths != INTERRUPTED_165534_STOCK_DIFFERENCE_PATHS):
            raise BackfillError("Invoice 165534 has unexpected business differences; halt retained")


def validate_165818_index_isolation(store, saved):
    marker = PENDING_CONTINUATION_165818_MARKER
    field_id = "1462401000020819910"
    schema = {"field_id": field_id, "api_name": "cf_customer_branch"}
    mapping = json.loads(saved["mapping"])
    if (saved["type"] != "invoice" or saved["zoho_id"] != marker["document_id"] or
            saved["status"] != "failed" or saved["error"] != marker["failure"] or
            not saved["original"] or mapping.get("number") != marker["number"] or
            mapping.get("branch_record_id") != marker["branch_record_id"]):
        raise BackfillError("Saved invoice 165818 failure evidence changed; halt retained")

    original = json.loads(saved["original"])
    if not (branch_is_omitted(original, field_id, schema) or
            branch_value(original, field_id) == ""):
        raise BackfillError("Saved invoice 165818 original branch is not blank; halt retained")

    prefix = "evidence:invoice:" + marker["document_id"] + ":"
    evidence = {}
    for item in store.db.execute("SELECT key,value FROM meta WHERE key LIKE ?",
                                 (prefix + "%",)):
        evidence.setdefault(item["key"].rsplit(":", 1)[-1], []).append(
            json.loads(item["value"]))
    expected_kinds = {"lookup_before", "request", "write_response", "readback",
                      "business_differences"}
    expected_request = {
        "method": "PUT", "path": "/invoices/" + marker["document_id"],
        "payload": {"custom_fields": [{
            "customfield_id": field_id, "value": marker["branch_record_id"]}]}}
    lookups = evidence.get("lookup_before", [])
    responses = evidence.get("write_response", [])
    readbacks = evidence.get("readback", [])
    differences = evidence.get("business_differences", [])
    if (set(evidence) != expected_kinds or evidence.get("request") != [expected_request] or
            len(lookups) != 1 or len(responses) != 1 or
            responses[0].get("code") != 0 or len(readbacks) != 1 or
            len(differences) != 1 or
            branch_value(lookups[0], field_id) != "" or
            branch_value(readbacks[0], field_id) != marker["branch_record_id"]):
        raise BackfillError("Saved invoice 165818 write/readback evidence is ambiguous; halt retained")

    actual_differences = document_differences(
        projection(original, field_id, schema),
        projection(readbacks[0], field_id, schema))
    if (len(actual_differences) != 1 or
            actual_differences[0].get("path") != "/custom_fields/0/index" or
            differences != [actual_differences]):
        raise BackfillError("Invoice 165818 has unexpected business differences; halt retained")


def validate_167764_unauthorized_isolation(store, saved):
    marker = PENDING_CONTINUATION_167764_MARKER
    field_id = "1462401000020819910"
    schema = {"field_id": field_id, "api_name": "cf_customer_branch"}

    def branch_is_empty(document):
        return (branch_is_omitted(document, field_id, schema) or
                branch_value(document, field_id) == "")

    mapping = json.loads(saved["mapping"])
    if (saved["type"] != "invoice" or saved["zoho_id"] != marker["document_id"] or
            saved["status"] != "failed" or saved["error"] != marker["failure"] or
            not saved["original"] or mapping.get("number") != marker["number"] or
            mapping.get("branch_record_id") != marker["branch_record_id"]):
        raise BackfillError("Saved invoice 167764 failure evidence changed; halt retained")

    original = json.loads(saved["original"])
    if not branch_is_empty(original):
        raise BackfillError("Saved invoice 167764 original branch is not blank; halt retained")

    prefix = "evidence:invoice:" + marker["document_id"] + ":"
    evidence = {}
    for item in store.db.execute("SELECT key,value FROM meta WHERE key LIKE ?",
                                 (prefix + "%",)):
        evidence.setdefault(item["key"].rsplit(":", 1)[-1], []).append(
            json.loads(item["value"]))
    expected_kinds = {"lookup_before", "request", "write_error", "readback",
                      "verification_lookup"}
    expected_request = {
        "method": "PUT", "path": "/invoices/" + marker["document_id"],
        "payload": {"custom_fields": [{
            "customfield_id": field_id, "value": marker["branch_record_id"]}]}}
    expected_error = {
        "error": "API rejected request (HTTP 401)", "http_status": 401,
        "response": {"code": 57,
                     "message": "You are not authorized to perform this operation"}}
    lookups = evidence.get("lookup_before", [])
    readbacks = evidence.get("readback", [])
    verification_lookups = evidence.get("verification_lookup", [])
    if (set(evidence) != expected_kinds or evidence.get("request") != [expected_request] or
            evidence.get("write_error") != [expected_error] or
            len(lookups) != 1 or len(readbacks) != 1 or
            len(verification_lookups) != 1 or
            branch_value(lookups[0], field_id) != "" or
            not branch_is_empty(readbacks[0]) or
            branch_value(verification_lookups[0], field_id) != "" or
            document_differences(
                projection(original, field_id, schema),
                projection(readbacks[0], field_id, schema))):
        raise BackfillError("Saved invoice 167764 authorization evidence is missing or ambiguous; halt retained")


def validate_168436_index_isolation(store, saved):
    marker = PENDING_CONTINUATION_168436_MARKER
    field_id = "1462401000020819910"
    schema = {"field_id": field_id, "api_name": "cf_customer_branch"}
    mapping = json.loads(saved["mapping"])
    if (saved["type"] != "invoice" or saved["zoho_id"] != marker["document_id"] or
            saved["status"] != "failed" or saved["error"] != marker["failure"] or
            not saved["original"] or mapping.get("number") != marker["number"] or
            mapping.get("branch_record_id") != marker["branch_record_id"]):
        raise BackfillError("Saved invoice 168436 failure evidence changed; halt retained")

    original = json.loads(saved["original"])
    if not (branch_is_omitted(original, field_id, schema) or
            branch_value(original, field_id) == ""):
        raise BackfillError("Saved invoice 168436 original branch is not blank; halt retained")

    prefix = "evidence:invoice:" + marker["document_id"] + ":"
    evidence = {}
    for item in store.db.execute("SELECT key,value FROM meta WHERE key LIKE ?",
                                 (prefix + "%",)):
        evidence.setdefault(item["key"].rsplit(":", 1)[-1], []).append(
            json.loads(item["value"]))
    expected_kinds = {"lookup_before", "request", "write_response", "readback",
                      "business_differences"}
    expected_request = {
        "method": "PUT", "path": "/invoices/" + marker["document_id"],
        "payload": {"custom_fields": [{
            "customfield_id": field_id, "value": marker["branch_record_id"]}]}}
    lookups = evidence.get("lookup_before", [])
    responses = evidence.get("write_response", [])
    readbacks = evidence.get("readback", [])
    differences = evidence.get("business_differences", [])
    if (set(evidence) != expected_kinds or evidence.get("request") != [expected_request] or
            len(lookups) != 1 or len(responses) != 1 or
            responses[0].get("code") != 0 or len(readbacks) != 1 or
            len(differences) != 1 or
            branch_value(lookups[0], field_id) != "" or
            branch_value(readbacks[0], field_id) != marker["branch_record_id"]):
        raise BackfillError("Saved invoice 168436 write/readback evidence is ambiguous; halt retained")

    actual_differences = document_differences(
        projection(original, field_id, schema),
        projection(readbacks[0], field_id, schema))
    expected_indices = {
        "/custom_fields/{}/index".format(index): (index + 1, index + 2)
        for index in range(6)
    }
    actual_indices = {
        difference.get("path"): (difference.get("before"), difference.get("after"))
        for difference in actual_differences
    }
    if (actual_indices != expected_indices or differences != [actual_differences]):
        raise BackfillError("Invoice 168436 has unexpected business differences; halt retained")


def validate_168777_index_isolation(store, saved):
    marker = PENDING_CONTINUATION_168777_MARKER
    field_id = "1462401000020819910"
    schema = {"field_id": field_id, "api_name": "cf_customer_branch"}
    mapping = json.loads(saved["mapping"])
    if (saved["type"] != "invoice" or saved["zoho_id"] != marker["document_id"] or
            saved["status"] != "failed" or saved["error"] != marker["failure"] or
            not saved["original"] or mapping.get("number") != marker["number"] or
            mapping.get("branch_record_id") != marker["branch_record_id"]):
        raise BackfillError("Saved invoice 168777 failure evidence changed; halt retained")

    original = json.loads(saved["original"])
    if not (branch_is_omitted(original, field_id, schema) or
            branch_value(original, field_id) == ""):
        raise BackfillError("Saved invoice 168777 original branch is not blank; halt retained")

    prefix = "evidence:invoice:" + marker["document_id"] + ":"
    evidence = {}
    for item in store.db.execute("SELECT key,value FROM meta WHERE key LIKE ?",
                                 (prefix + "%",)):
        evidence.setdefault(item["key"].rsplit(":", 1)[-1], []).append(
            json.loads(item["value"]))
    expected_kinds = {"lookup_before", "request", "write_response", "readback",
                      "business_differences"}
    expected_request = {
        "method": "PUT", "path": "/invoices/" + marker["document_id"],
        "payload": {"custom_fields": [{
            "customfield_id": field_id, "value": marker["branch_record_id"]}]}}
    lookups = evidence.get("lookup_before", [])
    responses = evidence.get("write_response", [])
    readbacks = evidence.get("readback", [])
    differences = evidence.get("business_differences", [])
    if (set(evidence) != expected_kinds or evidence.get("request") != [expected_request] or
            len(lookups) != 1 or len(responses) != 1 or
            responses[0].get("code") != 0 or len(readbacks) != 1 or
            len(differences) != 1 or
            branch_value(lookups[0], field_id) != "" or
            branch_value(readbacks[0], field_id) != marker["branch_record_id"]):
        raise BackfillError("Saved invoice 168777 write/readback evidence is ambiguous; halt retained")

    actual_differences = document_differences(
        projection(original, field_id, schema),
        projection(readbacks[0], field_id, schema))
    expected_difference = {
        "path": "/custom_fields/0/index", "before_present": True,
        "after_present": True, "before": 1, "after": 2,
    }
    if (actual_differences != [expected_difference] or
            differences != [actual_differences]):
        raise BackfillError("Invoice 168777 has unexpected business differences; halt retained")


def validate_168812_index_isolation(store, saved):
    marker = PENDING_CONTINUATION_168812_MARKER
    field_id = "1462401000020819910"
    schema = {"field_id": field_id, "api_name": "cf_customer_branch"}
    mapping = json.loads(saved["mapping"])
    if (saved["type"] != "invoice" or saved["zoho_id"] != marker["document_id"] or
            saved["status"] != "failed" or saved["error"] != marker["failure"] or
            not saved["original"] or mapping.get("number") != marker["number"] or
            mapping.get("branch_record_id") != marker["branch_record_id"]):
        raise BackfillError("Saved invoice 168812 failure evidence changed; halt retained")

    original = json.loads(saved["original"])
    if not (branch_is_omitted(original, field_id, schema) or
            branch_value(original, field_id) == ""):
        raise BackfillError("Saved invoice 168812 original branch is not blank; halt retained")

    prefix = "evidence:invoice:" + marker["document_id"] + ":"
    evidence = {}
    for item in store.db.execute("SELECT key,value FROM meta WHERE key LIKE ?",
                                 (prefix + "%",)):
        evidence.setdefault(item["key"].rsplit(":", 1)[-1], []).append(
            json.loads(item["value"]))
    expected_kinds = {"lookup_before", "request", "write_response", "readback",
                      "business_differences"}
    expected_request = {
        "method": "PUT", "path": "/invoices/" + marker["document_id"],
        "payload": {"custom_fields": [{
            "customfield_id": field_id, "value": marker["branch_record_id"]}]}}
    lookups = evidence.get("lookup_before", [])
    responses = evidence.get("write_response", [])
    readbacks = evidence.get("readback", [])
    differences = evidence.get("business_differences", [])
    if (set(evidence) != expected_kinds or evidence.get("request") != [expected_request] or
            len(lookups) != 1 or len(responses) != 1 or
            responses[0].get("code") != 0 or len(readbacks) != 1 or
            len(differences) != 1 or
            branch_value(lookups[0], field_id) != "" or
            branch_value(readbacks[0], field_id) != marker["branch_record_id"]):
        raise BackfillError("Saved invoice 168812 write/readback evidence is ambiguous; halt retained")

    actual_differences = document_differences(
        projection(original, field_id, schema),
        projection(readbacks[0], field_id, schema))
    expected_difference = {
        "path": "/custom_fields/0/index", "before_present": True,
        "after_present": True, "before": 1, "after": 2,
    }
    if (actual_differences != [expected_difference] or
            differences != [actual_differences]):
        raise BackfillError("Invoice 168812 has unexpected business differences; halt retained")


def validate_168905_index_isolation(store, saved):
    marker = PENDING_CONTINUATION_168905_MARKER
    field_id = "1462401000020819910"
    schema = {"field_id": field_id, "api_name": "cf_customer_branch"}
    mapping = json.loads(saved["mapping"])
    if (saved["type"] != "invoice" or saved["zoho_id"] != marker["document_id"] or
            saved["status"] != "failed" or saved["error"] != marker["failure"] or
            not saved["original"] or mapping.get("number") != marker["number"] or
            mapping.get("branch_record_id") != marker["branch_record_id"]):
        raise BackfillError("Saved invoice 168905 failure evidence changed; halt retained")

    original = json.loads(saved["original"])
    if not (branch_is_omitted(original, field_id, schema) or
            branch_value(original, field_id) == ""):
        raise BackfillError("Saved invoice 168905 original branch is not blank; halt retained")

    prefix = "evidence:invoice:" + marker["document_id"] + ":"
    evidence = {}
    for item in store.db.execute("SELECT key,value FROM meta WHERE key LIKE ?",
                                 (prefix + "%",)):
        evidence.setdefault(item["key"].rsplit(":", 1)[-1], []).append(
            json.loads(item["value"]))
    expected_kinds = {"lookup_before", "request", "write_response", "readback",
                      "business_differences"}
    expected_request = {
        "method": "PUT", "path": "/invoices/" + marker["document_id"],
        "payload": {"custom_fields": [{
            "customfield_id": field_id, "value": marker["branch_record_id"]}]}}
    lookups = evidence.get("lookup_before", [])
    responses = evidence.get("write_response", [])
    readbacks = evidence.get("readback", [])
    differences = evidence.get("business_differences", [])
    if (set(evidence) != expected_kinds or evidence.get("request") != [expected_request] or
            len(lookups) != 1 or len(responses) != 1 or
            responses[0].get("code") != 0 or len(readbacks) != 1 or
            len(differences) != 1 or
            branch_value(lookups[0], field_id) != "" or
            branch_value(readbacks[0], field_id) != marker["branch_record_id"]):
        raise BackfillError("Saved invoice 168905 write/readback evidence is ambiguous; halt retained")

    actual_differences = document_differences(
        projection(original, field_id, schema),
        projection(readbacks[0], field_id, schema))
    expected_difference = {
        "path": "/custom_fields/0/index", "before_present": True,
        "after_present": True, "before": 1, "after": 2,
    }
    if (actual_differences != [expected_difference] or
            differences != [actual_differences]):
        raise BackfillError("Invoice 168905 has unexpected business differences; halt retained")


def prepare_pending_continuation(store, isolate_stock_failure=False,
                                 isolate_unauthorized_failure=False,
                                 isolate_interrupted_stock_failure=False,
                                 isolate_165818_index_failure=False,
                                 isolate_167764_unauthorized_failure=False,
                                 isolate_168436_index_failure=False,
                                 isolate_168777_index_failure=False,
                                 isolate_168812_index_failure=False,
                                 isolate_168905_index_failure=False):
    halt = PENDING_CONTINUATION_MARKER["halt_reason"]
    row_id = PENDING_CONTINUATION_MARKER["document_id"]
    saved_marker = store.get(PENDING_CONTINUATION_KEY)
    if saved_marker is not None and saved_marker != PENDING_CONTINUATION_MARKER:
        raise BackfillError("Saved pending-continuation evidence changed; halt retained")
    stock_marker = store.get(PENDING_CONTINUATION_160059_KEY)
    if stock_marker is not None and stock_marker != PENDING_CONTINUATION_160059_MARKER:
        raise BackfillError("Saved invoice 160059 isolation evidence changed; halt retained")
    unauthorized_marker = store.get(PENDING_CONTINUATION_161528_KEY)
    if (unauthorized_marker is not None and
            unauthorized_marker != PENDING_CONTINUATION_161528_MARKER):
        raise BackfillError("Saved invoice 161528 isolation evidence changed; halt retained")
    interrupted_marker = store.get(PENDING_CONTINUATION_165534_KEY)
    if (interrupted_marker is not None and
            interrupted_marker != PENDING_CONTINUATION_165534_MARKER):
        raise BackfillError("Saved invoice 165534 isolation evidence changed; halt retained")
    index_marker = store.get(PENDING_CONTINUATION_165818_KEY)
    if index_marker is not None and index_marker != PENDING_CONTINUATION_165818_MARKER:
        raise BackfillError("Saved invoice 165818 isolation evidence changed; halt retained")
    unauthorized_167764_marker = store.get(PENDING_CONTINUATION_167764_KEY)
    if (unauthorized_167764_marker is not None and
            unauthorized_167764_marker != PENDING_CONTINUATION_167764_MARKER):
        raise BackfillError("Saved invoice 167764 isolation evidence changed; halt retained")
    index_168436_marker = store.get(PENDING_CONTINUATION_168436_KEY)
    if (index_168436_marker is not None and
            index_168436_marker != PENDING_CONTINUATION_168436_MARKER):
        raise BackfillError("Saved invoice 168436 isolation evidence changed; halt retained")
    index_168777_marker = store.get(PENDING_CONTINUATION_168777_KEY)
    if (index_168777_marker is not None and
            index_168777_marker != PENDING_CONTINUATION_168777_MARKER):
        raise BackfillError("Saved invoice 168777 isolation evidence changed; halt retained")
    index_168812_marker = store.get(PENDING_CONTINUATION_168812_KEY)
    if (index_168812_marker is not None and
            index_168812_marker != PENDING_CONTINUATION_168812_MARKER):
        raise BackfillError("Saved invoice 168812 isolation evidence changed; halt retained")
    index_168905_marker = store.get(PENDING_CONTINUATION_168905_KEY)
    if (index_168905_marker is not None and
            index_168905_marker != PENDING_CONTINUATION_168905_MARKER):
        raise BackfillError("Saved invoice 168905 isolation evidence changed; halt retained")
    active_halt = store.get("halted")
    if isolate_unauthorized_failure:
        if (saved_marker != PENDING_CONTINUATION_MARKER or
                stock_marker != PENDING_CONTINUATION_160059_MARKER or
                (unauthorized_marker is None and
                 active_halt != PENDING_CONTINUATION_161528_MARKER["halt_reason"]) or
                (unauthorized_marker is not None and active_halt is not None)):
            raise BackfillError("Invoice 161528 isolation requires its exact saved halt and prior isolation markers")
    elif isolate_stock_failure:
        if (saved_marker != PENDING_CONTINUATION_MARKER or
                (stock_marker is None and
                 active_halt != STOCK_ONLY_RECONCILIATION["halt_reason"]) or
                (stock_marker is not None and active_halt is not None)):
            raise BackfillError("Invoice 160059 isolation requires its exact saved halt and 157368 marker")
    elif isolate_interrupted_stock_failure:
        if (saved_marker != PENDING_CONTINUATION_MARKER or
                stock_marker != PENDING_CONTINUATION_160059_MARKER or
                active_halt is not None):
            raise BackfillError("Invoice 165534 isolation requires the existing 157368/160059 markers and no active halt")
    elif isolate_165818_index_failure:
        if (saved_marker != PENDING_CONTINUATION_MARKER or
                stock_marker != PENDING_CONTINUATION_160059_MARKER or
                unauthorized_marker != PENDING_CONTINUATION_161528_MARKER or
                interrupted_marker != PENDING_CONTINUATION_165534_MARKER or
                (index_marker is None and
                 active_halt != PENDING_CONTINUATION_165818_MARKER["halt_reason"]) or
                (index_marker is not None and active_halt is not None)):
            raise BackfillError("Invoice 165818 isolation requires its exact halt and prior isolation markers")
    elif isolate_167764_unauthorized_failure:
        if (saved_marker != PENDING_CONTINUATION_MARKER or
                stock_marker != PENDING_CONTINUATION_160059_MARKER or
                unauthorized_marker != PENDING_CONTINUATION_161528_MARKER or
                interrupted_marker != PENDING_CONTINUATION_165534_MARKER or
                index_marker != PENDING_CONTINUATION_165818_MARKER or
                (unauthorized_167764_marker is None and
                 active_halt != PENDING_CONTINUATION_167764_MARKER["halt_reason"]) or
                (unauthorized_167764_marker is not None and active_halt is not None)):
            raise BackfillError("Invoice 167764 isolation requires its exact halt and prior isolation markers")
    elif isolate_168436_index_failure:
        if (saved_marker != PENDING_CONTINUATION_MARKER or
                stock_marker != PENDING_CONTINUATION_160059_MARKER or
                unauthorized_marker != PENDING_CONTINUATION_161528_MARKER or
                interrupted_marker != PENDING_CONTINUATION_165534_MARKER or
                index_marker != PENDING_CONTINUATION_165818_MARKER or
                unauthorized_167764_marker != PENDING_CONTINUATION_167764_MARKER or
                (index_168436_marker is None and
                 active_halt != PENDING_CONTINUATION_168436_MARKER["halt_reason"]) or
                (index_168436_marker is not None and active_halt is not None)):
            raise BackfillError("Invoice 168436 isolation requires its exact halt and prior isolation markers")
    elif isolate_168777_index_failure:
        if (saved_marker != PENDING_CONTINUATION_MARKER or
                stock_marker != PENDING_CONTINUATION_160059_MARKER or
                unauthorized_marker != PENDING_CONTINUATION_161528_MARKER or
                interrupted_marker != PENDING_CONTINUATION_165534_MARKER or
                index_marker != PENDING_CONTINUATION_165818_MARKER or
                unauthorized_167764_marker != PENDING_CONTINUATION_167764_MARKER or
                index_168436_marker != PENDING_CONTINUATION_168436_MARKER or
                (index_168777_marker is None and
                 active_halt != PENDING_CONTINUATION_168777_MARKER["halt_reason"]) or
                (index_168777_marker is not None and active_halt is not None)):
            raise BackfillError("Invoice 168777 isolation requires its exact halt and prior isolation markers")
    elif isolate_168812_index_failure:
        if (saved_marker != PENDING_CONTINUATION_MARKER or
                stock_marker != PENDING_CONTINUATION_160059_MARKER or
                unauthorized_marker != PENDING_CONTINUATION_161528_MARKER or
                interrupted_marker != PENDING_CONTINUATION_165534_MARKER or
                index_marker != PENDING_CONTINUATION_165818_MARKER or
                unauthorized_167764_marker != PENDING_CONTINUATION_167764_MARKER or
                index_168436_marker != PENDING_CONTINUATION_168436_MARKER or
                index_168777_marker != PENDING_CONTINUATION_168777_MARKER or
                (index_168812_marker is None and
                 active_halt != PENDING_CONTINUATION_168812_MARKER["halt_reason"]) or
                (index_168812_marker is not None and active_halt is not None)):
            raise BackfillError("Invoice 168812 isolation requires its exact halt and prior isolation markers")
    elif isolate_168905_index_failure:
        if (saved_marker != PENDING_CONTINUATION_MARKER or
                stock_marker != PENDING_CONTINUATION_160059_MARKER or
                unauthorized_marker != PENDING_CONTINUATION_161528_MARKER or
                interrupted_marker != PENDING_CONTINUATION_165534_MARKER or
                index_marker != PENDING_CONTINUATION_165818_MARKER or
                unauthorized_167764_marker != PENDING_CONTINUATION_167764_MARKER or
                index_168436_marker != PENDING_CONTINUATION_168436_MARKER or
                index_168777_marker != PENDING_CONTINUATION_168777_MARKER or
                index_168812_marker != PENDING_CONTINUATION_168812_MARKER or
                (index_168905_marker is None and
                 active_halt != PENDING_CONTINUATION_168905_MARKER["halt_reason"]) or
                (index_168905_marker is not None and active_halt is not None)):
            raise BackfillError("Invoice 168905 isolation requires its exact halt and prior isolation markers")
    elif stock_marker is not None:
        if active_halt is not None:
            raise BackfillError("A new halt is active; investigate it before continuing")
    elif ((saved_marker is None and active_halt != halt) or
            (saved_marker is not None and active_halt not in (halt, None))):
        raise BackfillError("A different halt is active; investigate it before continuing")

    failed = list(store.db.execute("SELECT * FROM documents WHERE status='failed'"))
    target_157368 = [row for row in failed
                     if row["type"] == "invoice" and row["zoho_id"] == row_id]
    allowed_failures = {row_id}
    if isolate_stock_failure or stock_marker is not None:
        allowed_failures.add(PENDING_CONTINUATION_160059_MARKER["document_id"])
    if isolate_unauthorized_failure or unauthorized_marker is not None:
        allowed_failures.add(PENDING_CONTINUATION_161528_MARKER["document_id"])
    interrupted_id = PENDING_CONTINUATION_165534_MARKER["document_id"]
    interrupted = store.db.execute(
        "SELECT * FROM documents WHERE type='invoice' AND zoho_id=?",
        (interrupted_id,)).fetchone()
    if isolate_interrupted_stock_failure or interrupted_marker is not None:
        if interrupted is None:
            raise BackfillError("Saved invoice 165534 isolation row is missing; halt retained")
        validate_interrupted_165534_isolation(
            store, interrupted, already_isolated=interrupted_marker is not None)
        allowed_failures.add(interrupted_id)
    index_id = PENDING_CONTINUATION_165818_MARKER["document_id"]
    index_failure = store.db.execute(
        "SELECT * FROM documents WHERE type='invoice' AND zoho_id=?",
        (index_id,)).fetchone()
    if isolate_165818_index_failure or index_marker is not None:
        if index_failure is None:
            raise BackfillError("Saved invoice 165818 isolation row is missing; halt retained")
        validate_165818_index_isolation(store, index_failure)
        allowed_failures.add(index_id)
    unauthorized_167764_id = PENDING_CONTINUATION_167764_MARKER["document_id"]
    unauthorized_167764 = store.db.execute(
        "SELECT * FROM documents WHERE type='invoice' AND zoho_id=?",
        (unauthorized_167764_id,)).fetchone()
    if isolate_167764_unauthorized_failure or unauthorized_167764_marker is not None:
        if unauthorized_167764 is None:
            raise BackfillError("Saved invoice 167764 isolation row is missing; halt retained")
        validate_167764_unauthorized_isolation(store, unauthorized_167764)
        allowed_failures.add(unauthorized_167764_id)
    index_168436_id = PENDING_CONTINUATION_168436_MARKER["document_id"]
    index_168436_failure = store.db.execute(
        "SELECT * FROM documents WHERE type='invoice' AND zoho_id=?",
        (index_168436_id,)).fetchone()
    if isolate_168436_index_failure or index_168436_marker is not None:
        if index_168436_failure is None:
            raise BackfillError("Saved invoice 168436 isolation row is missing; halt retained")
        validate_168436_index_isolation(store, index_168436_failure)
        allowed_failures.add(index_168436_id)
    index_168777_id = PENDING_CONTINUATION_168777_MARKER["document_id"]
    index_168777_failure = store.db.execute(
        "SELECT * FROM documents WHERE type='invoice' AND zoho_id=?",
        (index_168777_id,)).fetchone()
    if isolate_168777_index_failure or index_168777_marker is not None:
        if index_168777_failure is None:
            raise BackfillError("Saved invoice 168777 isolation row is missing; halt retained")
        validate_168777_index_isolation(store, index_168777_failure)
        allowed_failures.add(index_168777_id)
    index_168812_id = PENDING_CONTINUATION_168812_MARKER["document_id"]
    index_168812_failure = store.db.execute(
        "SELECT * FROM documents WHERE type='invoice' AND zoho_id=?",
        (index_168812_id,)).fetchone()
    if isolate_168812_index_failure or index_168812_marker is not None:
        if index_168812_failure is None:
            raise BackfillError("Saved invoice 168812 isolation row is missing; halt retained")
        validate_168812_index_isolation(store, index_168812_failure)
        allowed_failures.add(index_168812_id)
    index_168905_id = PENDING_CONTINUATION_168905_MARKER["document_id"]
    index_168905_failure = store.db.execute(
        "SELECT * FROM documents WHERE type='invoice' AND zoho_id=?",
        (index_168905_id,)).fetchone()
    if isolate_168905_index_failure or index_168905_marker is not None:
        if index_168905_failure is None:
            raise BackfillError("Saved invoice 168905 isolation row is missing; halt retained")
        validate_168905_index_isolation(store, index_168905_failure)
        allowed_failures.add(index_168905_id)
    failed_ids = {row["zoho_id"] for row in failed}
    if isolate_interrupted_stock_failure and interrupted_marker is None:
        failed_ids.add(interrupted_id)
    if (len(target_157368) != 1 or
            failed_ids != allowed_failures or
            not target_157368[0]["original"] or
            target_157368[0]["error"] != PENDING_CONTINUATION_MARKER["failure"]):
        raise BackfillError("Continuation requires only the explicitly isolated failed invoices; halt retained")
    mapping = json.loads(target_157368[0]["mapping"])
    if (mapping.get("number") != PENDING_CONTINUATION_MARKER["number"] or
            mapping.get("branch_record_id") != PENDING_CONTINUATION_MARKER["branch_record_id"]):
        raise BackfillError("Saved invoice 157368 mapping changed; halt retained")

    expected_request = {
        "method": "PUT", "path": "/invoice/" + row_id + "/customfields",
        "payload": {"custom_fields": [{"customfield_id": "1462401000020819910",
                                       "value": PENDING_CONTINUATION_MARKER["branch_record_id"]}]}}
    expected_rejection = {
        "error": "API rejected request (HTTP 401)", "http_status": 401,
        "response": {"code": 57,
                     "message": "You are not authorized to perform this operation"}}
    request_evidence, rejection_evidence, write_responses = [], [], []
    for item in store.db.execute("SELECT key,value FROM meta WHERE key LIKE ?",
                                 ("evidence:invoice:" + row_id + ":%",)):
        kind = item["key"].rsplit(":", 1)[-1]
        if kind == "customfield_endpoint_retry_request":
            request_evidence.append(json.loads(item["value"]))
        elif kind == "customfield_endpoint_retry_write_error":
            rejection_evidence.append(json.loads(item["value"]))
        elif kind == "customfield_endpoint_retry_write_response":
            write_responses.append(json.loads(item["value"]))
    if (request_evidence != [expected_request] or
            rejection_evidence != [expected_rejection] or write_responses):
        raise BackfillError("Saved 157368 authorization evidence is missing or ambiguous; halt retained")

    if isolate_stock_failure or stock_marker is not None:
        stock_id = PENDING_CONTINUATION_160059_MARKER["document_id"]
        stock_failed = next((row for row in failed if row["zoho_id"] == stock_id), None)
        if (stock_failed is None or not stock_failed["original"] or
                stock_failed["error"] != PENDING_CONTINUATION_160059_MARKER["failure"]):
            raise BackfillError("Saved invoice 160059 failure is missing or changed; halt retained")
        stock_mapping = json.loads(stock_failed["mapping"])
        if (stock_mapping.get("number") != PENDING_CONTINUATION_160059_MARKER["number"] or
                stock_mapping.get("branch_record_id") !=
                    PENDING_CONTINUATION_160059_MARKER["branch_record_id"]):
            raise BackfillError("Saved invoice 160059 mapping changed; halt retained")

        stock_evidence = {}
        for item in store.db.execute("SELECT key,value FROM meta WHERE key LIKE ?",
                                     ("evidence:invoice:" + stock_id + ":%",)):
            stock_evidence.setdefault(item["key"].rsplit(":", 1)[-1], []).append(
                json.loads(item["value"]))
        expected_stock_request = {
            "method": "PUT", "path": "/invoices/" + stock_id, "payload": {
                "custom_fields": [{"customfield_id": "1462401000020819910",
                                   "value": PENDING_CONTINUATION_160059_MARKER["branch_record_id"]}]}}
        write_responses = stock_evidence.get("write_response", [])
        readbacks = stock_evidence.get("readback", [])
        initial_differences = stock_evidence.get("business_differences", [])
        expected_paths = STOCK_ONLY_DIFFERENCE_PATHS
        if (stock_evidence.get("request") != [expected_stock_request] or
                len(write_responses) != 1 or write_responses[0].get("code") != 0 or
                len(readbacks) != 1 or
                branch_value(readbacks[0], "1462401000020819910") !=
                    PENDING_CONTINUATION_160059_MARKER["branch_record_id"] or
                len(initial_differences) != 1 or
                {item.get("path") for item in initial_differences[0]} != expected_paths):
            raise BackfillError("Saved invoice 160059 write evidence is missing or ambiguous; halt retained")

        later_differences = stock_evidence.get("stock_only_reconciliation_differences", [])
        later_reads = stock_evidence.get("stock_only_reconciliation_readback", [])
        has_additional_changes = any(
            isinstance(group, list) and any(
                isinstance(item, dict) and item.get("path") not in expected_paths
                for item in group)
            for entry in later_differences if isinstance(entry, dict)
            for group in (entry.get("from_original"), entry.get("from_saved_readback")))
        if not later_reads or not has_additional_changes:
            raise BackfillError("Saved invoice 160059 investigation does not prove additional changes; halt retained")

    if isolate_unauthorized_failure or unauthorized_marker is not None:
        unauthorized_id = PENDING_CONTINUATION_161528_MARKER["document_id"]
        unauthorized_failed = next(
            (row for row in failed if row["zoho_id"] == unauthorized_id), None)
        if (unauthorized_failed is None or not unauthorized_failed["original"] or
                unauthorized_failed["error"] != PENDING_CONTINUATION_161528_MARKER["failure"]):
            raise BackfillError("Saved invoice 161528 failure is missing or changed; halt retained")
        unauthorized_mapping = json.loads(unauthorized_failed["mapping"])
        if (unauthorized_mapping.get("number") != PENDING_CONTINUATION_161528_MARKER["number"] or
                unauthorized_mapping.get("branch_record_id") !=
                    PENDING_CONTINUATION_161528_MARKER["branch_record_id"]):
            raise BackfillError("Saved invoice 161528 mapping changed; halt retained")

        unauthorized_evidence = {}
        for item in store.db.execute("SELECT key,value FROM meta WHERE key LIKE ?",
                ("evidence:invoice:" + unauthorized_id + ":%",)):
            unauthorized_evidence.setdefault(item["key"].rsplit(":", 1)[-1], []).append(
                json.loads(item["value"]))
        expected_write = {
            "method": "PUT", "path": "/invoices/" + unauthorized_id, "payload": {
                "custom_fields": [{"customfield_id": "1462401000020819910",
                                   "value": PENDING_CONTINUATION_161528_MARKER["branch_record_id"]}]}}
        expected_error = {
            "error": "API rejected request (HTTP 401)", "http_status": 401,
            "response": {"code": 57,
                         "message": "You are not authorized to perform this operation"}}
        lookup_before = unauthorized_evidence.get("lookup_before", [])
        readbacks = unauthorized_evidence.get("readback", [])
        verification_lookup = unauthorized_evidence.get("verification_lookup", [])
        invoice_schema = {"field_id": "1462401000020819910",
                          "api_name": "cf_customer_branch"}
        original = json.loads(unauthorized_failed["original"])
        if (unauthorized_evidence.get("request") != [expected_write] or
                unauthorized_evidence.get("write_error") != [expected_error] or
                unauthorized_evidence.get("write_response") or
                len(lookup_before) != 1 or len(readbacks) != 1 or
                len(verification_lookup) != 1 or
                branch_value(lookup_before[0], invoice_schema["field_id"]) != "" or
                branch_value(verification_lookup[0], invoice_schema["field_id"]) != "" or
                document_differences(
                    projection(original, invoice_schema["field_id"], invoice_schema),
                    projection(readbacks[0], invoice_schema["field_id"], invoice_schema))):
            raise BackfillError("Saved invoice 161528 authorization evidence is missing or ambiguous; halt retained")

    if (saved_marker is None or
            (isolate_stock_failure and stock_marker is None) or
            (isolate_unauthorized_failure and unauthorized_marker is None) or
            (isolate_interrupted_stock_failure and interrupted_marker is None) or
            (isolate_165818_index_failure and index_marker is None) or
            (isolate_167764_unauthorized_failure and unauthorized_167764_marker is None) or
            (isolate_168436_index_failure and index_168436_marker is None) or
            (isolate_168777_index_failure and index_168777_marker is None) or
            (isolate_168812_index_failure and index_168812_marker is None) or
            (isolate_168905_index_failure and index_168905_marker is None)):
        with store.db:
            if saved_marker is None:
                store.put(PENDING_CONTINUATION_KEY, PENDING_CONTINUATION_MARKER)
            if isolate_stock_failure and stock_marker is None:
                store.put(PENDING_CONTINUATION_160059_KEY,
                          PENDING_CONTINUATION_160059_MARKER)
                store.put("halted", None)
            if isolate_unauthorized_failure and unauthorized_marker is None:
                store.put(PENDING_CONTINUATION_161528_KEY,
                          PENDING_CONTINUATION_161528_MARKER)
                store.put("halted", None)
            if isolate_interrupted_stock_failure and interrupted_marker is None:
                updated = store.db.execute(
                    "UPDATE documents SET status='failed',error=? "
                    "WHERE type='invoice' AND zoho_id=? AND status='updating'",
                    (PENDING_CONTINUATION_165534_MARKER["failure"], interrupted_id))
                if updated.rowcount != 1:
                    raise BackfillError("Invoice 165534 state changed during isolation; halt retained")
                store.put(PENDING_CONTINUATION_165534_KEY,
                          PENDING_CONTINUATION_165534_MARKER)
            if isolate_165818_index_failure and index_marker is None:
                store.put(PENDING_CONTINUATION_165818_KEY,
                          PENDING_CONTINUATION_165818_MARKER)
                store.put("halted", None)
            if (isolate_167764_unauthorized_failure and
                    unauthorized_167764_marker is None):
                store.put(PENDING_CONTINUATION_167764_KEY,
                          PENDING_CONTINUATION_167764_MARKER)
                store.put("halted", None)
            if isolate_168436_index_failure and index_168436_marker is None:
                store.put(PENDING_CONTINUATION_168436_KEY,
                          PENDING_CONTINUATION_168436_MARKER)
                store.put("halted", None)
            if isolate_168777_index_failure and index_168777_marker is None:
                store.put(PENDING_CONTINUATION_168777_KEY,
                          PENDING_CONTINUATION_168777_MARKER)
                store.put("halted", None)
            if isolate_168812_index_failure and index_168812_marker is None:
                store.put(PENDING_CONTINUATION_168812_KEY,
                          PENDING_CONTINUATION_168812_MARKER)
                store.put("halted", None)
            if isolate_168905_index_failure and index_168905_marker is None:
                store.put(PENDING_CONTINUATION_168905_KEY,
                          PENDING_CONTINUATION_168905_MARKER)
                store.put("halted", None)


def should_process_invoice(status, continue_pending):
    if continue_pending:
        return status == "pending"
    return status not in ("updated_verified", "already_correct", "conflict")


def progress_line(checked, batch_total, summary):
    verified = summary["updated_verified"] + summary["already_correct"]
    total = summary["total_mapped"]
    percent = 100 * verified / total if total else 0
    return ("Progress: batch {:,}/{:,}; verified {:,}/{:,} ({:.1f}%); {:,} pending."
            .format(checked, batch_total, verified, total, percent, summary["pending"]))


def should_verify_investigated_invoice(continue_pending, reconcile_stock_only,
                                       journal_exists, reconcile_read_failure=False,
                                       reconcile_manual_branch=False):
    return not ((continue_pending or reconcile_stock_only or reconcile_read_failure or
                 reconcile_manual_branch) and journal_exists)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--test", action="store_true", help="Stop after one successful changed invoice")
    mode.add_argument("--run", action="store_true", help="Resume invoices after the isolated test succeeds")
    mode.add_argument("--continue-pending", action="store_true",
                      help="Continue pending invoices while preserving all explicitly isolated invoice failures")
    mode.add_argument("--isolate-160059-and-continue", action="store_true",
                      help="Explicitly isolate the investigated 160059 failure, then process pending invoices only")
    mode.add_argument("--isolate-161528-and-continue", action="store_true",
                      help="Isolate the saved 161528 authorization rejection, then process pending invoices only")
    mode.add_argument("--isolate-165534-and-continue", action="store_true",
                      help="Keep interrupted invoice 165534 failed and unverified, then process pending invoices only")
    mode.add_argument("--isolate-165818-and-continue", action="store_true",
                      help="Keep invoice 165818 failed and unverified after its exact index-only difference, then process pending invoices")
    mode.add_argument("--isolate-167764-and-continue", action="store_true",
                      help="Keep invoice 167764 failed and unverified after its exact authorization rejection, then process pending invoices")
    mode.add_argument("--isolate-168436-and-continue", action="store_true",
                      help="Keep invoice 168436 failed and unverified after its exact index shifts, then process pending invoices")
    mode.add_argument("--isolate-168777-and-continue", action="store_true",
                      help="Keep invoice 168777 failed and unverified after its exact index-only difference, then process pending invoices")
    mode.add_argument("--isolate-168812-and-continue", action="store_true",
                      help="Keep invoice 168812 failed and unverified after its exact index-only difference, then process pending invoices")
    mode.add_argument("--isolate-168905-and-continue", action="store_true",
                      help="Keep invoice 168905 failed and unverified after its exact index-only difference, then process pending invoices")
    mode.add_argument("--reconcile-read-failure", action="store_true",
                      help="Read and verify a single successful or uncertain write halted by exhausted read retries")
    mode.add_argument("--reconcile-429", action="store_true",
                      help="Fresh-read invoice 156416 and retry only if its branch is blank and business data is unchanged")
    mode.add_argument("--reconcile-omitted-field", action="store_true",
                      help="Safely reconcile the uncertain Customer Branch update on invoice 157368")
    mode.add_argument("--retry-inactive-template", action="store_true",
                      help="Retry invoice 157368 once after a saved inactive-template rejection")
    mode.add_argument("--retry-dedicated-customfield", action="store_true",
                      help="Retry invoice 157368 using Zoho's dedicated custom-field endpoint")
    mode.add_argument("--reconcile-manual-branch", action="store_true",
                      help="Verify a manually saved branch on invoice 157368 without writing")
    mode.add_argument("--reconcile-stock-only", action="store_true",
                      help="Fresh-read invoice 160059 and reconcile only its five stock-availability differences")
    parser.add_argument("--confirm-template-active", action="store_true",
                        help="Confirm HQ Template is active in the approved Zoho organisation")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--requests-per-minute", type=int, default=90,
                        help="Request cap 1..90; default 90 (previously 85)")
    args = parser.parse_args()
    continue_pending = (args.continue_pending or args.isolate_160059_and_continue or
                       args.isolate_161528_and_continue or
                       args.isolate_165534_and_continue or
                       args.isolate_165818_and_continue or
                       args.isolate_167764_and_continue or
                       args.isolate_168436_and_continue or
                       args.isolate_168777_and_continue or
                       args.isolate_168812_and_continue or
                       args.isolate_168905_and_continue)
    if args.limit is None:
        args.limit = 100 if continue_pending else 14450
    if not 1 <= args.limit <= 14450:
        raise BackfillError("Limit must be 1..14450")
    if not 1 <= args.requests_per_minute <= 90:
        raise BackfillError("Request cap must be 1..90 per minute")
    if args.retry_inactive_template != args.confirm_template_active:
        raise BackfillError("--retry-inactive-template requires --confirm-template-active")
    rows, fingerprint = load_map("data/Armco_Branch_Backfill_API_Map_corrected.csv")
    cfg = settings(["invoice"])
    if cfg["ZOHO_ORG_ID"] != "941141787" or cfg["CF_INVOICE_ID"] != "1462401000020819910":
        raise BackfillError("Invoice settings differ from approved organisation/field")
    root = Path("output/corrected_run")
    invoice_journal = root / "invoice_field_id_run" / "progress.sqlite3"
    with run_lock(root):
        # Investigated old halt remains untouched. Require its repaired manual test
        # invoice to still match original business data before creating the new journal.
        with contextlib.closing(sqlite3.connect((root / "progress.sqlite3").resolve().as_uri() + "?mode=ro", uri=True)) as db:
            identity = json.loads(db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()[0])
            old = db.execute("SELECT mapping,original FROM documents WHERE type='invoice' AND zoho_id='1462401000001974003'").fetchone()
        for key in ("ZOHO_ORG_ID", "ZOHO_ACCOUNTS", "ZOHO_API", "CF_INVOICE_ID"):
            if cfg[key] != identity.get(key):
                raise BackfillError("Settings differ from original invoice run")
        if fingerprint not in identity.values() or not old or not old[1]:
            raise BackfillError("Original approved map/snapshot missing")
        client = Client(cfg, delay=0, requests_per_minute=args.requests_per_minute)
        schema = validate_lookup_schema(client.request("GET", "/settings/fields", query={"entity": "invoice"}),
                                        cfg["CF_INVOICE_ID"], "invoice")
        old_row, original = json.loads(old[0]), json.loads(old[1])
        if should_verify_investigated_invoice(
                continue_pending, args.reconcile_stock_only, invoice_journal.is_file(),
                args.reconcile_read_failure, args.reconcile_manual_branch):
            repaired = client.read(old_row)
            if (projection(original, cfg["CF_INVOICE_ID"], schema) != projection(repaired, cfg["CF_INVOICE_ID"], schema) or
                    branch_value(repaired, cfg["CF_INVOICE_ID"]) != old_row["branch_record_id"]):
                raise BackfillError("Investigated invoice repair does not verify; original halt retained")
        folder = root / "invoice_field_id_run"
        folder.mkdir(exist_ok=True)
        invoices = [r for r in rows if r["type"] == "invoice"]
        store = Store(folder / "progress.sqlite3", invoices,
                      {**{k: cfg[k] for k in ("ZOHO_ORG_ID", "ZOHO_ACCOUNTS", "ZOHO_API", "CF_INVOICE_ID")},
                       "map_sha256": fingerprint, "protocol": "invoice-field-id-v1"})
        try:
            if args.reconcile_read_failure:
                reconcile_read_failure(client, store, schema, folder)
                print(dumps(store.report(folder)), flush=True)
                return
            if args.reconcile_429:
                reconcile_429(client, store, schema, folder)
                print(dumps(store.report(folder)), flush=True)
                return
            if args.reconcile_omitted_field:
                reconcile_omitted_field(client, store, schema, folder)
                print(dumps(store.report(folder)), flush=True)
                return
            if args.retry_inactive_template:
                retry_inactive_template_rejection(client, store, schema, folder,
                                                  args.confirm_template_active)
                print(dumps(store.report(folder)), flush=True)
                return
            if args.retry_dedicated_customfield:
                retry_dedicated_customfield_endpoint(client, store, schema, folder)
                print(dumps(store.report(folder)), flush=True)
                return
            if args.reconcile_manual_branch:
                reconcile_manual_branch_update(client, store, schema, folder)
                print(dumps(store.report(folder)), flush=True)
                return
            if args.reconcile_stock_only:
                reconcile_stock_only_difference(client, store, schema, folder)
                print(dumps(store.report(folder)), flush=True)
                return
            if continue_pending:
                prepare_pending_continuation(
                    store, isolate_stock_failure=args.isolate_160059_and_continue,
                    isolate_unauthorized_failure=args.isolate_161528_and_continue,
                    isolate_interrupted_stock_failure=args.isolate_165534_and_continue,
                    isolate_165818_index_failure=args.isolate_165818_and_continue,
                    isolate_167764_unauthorized_failure=args.isolate_167764_and_continue,
                    isolate_168436_index_failure=args.isolate_168436_and_continue,
                    isolate_168777_index_failure=args.isolate_168777_and_continue,
                    isolate_168812_index_failure=args.isolate_168812_and_continue,
                    isolate_168905_index_failure=args.isolate_168905_and_continue)
            else:
                marker = store.get(PENDING_CONTINUATION_KEY)
                if marker == PENDING_CONTINUATION_MARKER:
                    isolated = store.db.execute(
                        "SELECT status FROM documents WHERE type='invoice' AND zoho_id=?",
                        (PENDING_CONTINUATION_MARKER["document_id"],)).fetchone()
                    if isolated and isolated["status"] == "failed":
                        raise BackfillError("Invoice 157368 remains isolated; use a pending-only continuation mode")
                stock_marker = store.get(PENDING_CONTINUATION_160059_KEY)
                if stock_marker == PENDING_CONTINUATION_160059_MARKER:
                    isolated = store.db.execute(
                        "SELECT status FROM documents WHERE type='invoice' AND zoho_id=?",
                        (PENDING_CONTINUATION_160059_MARKER["document_id"],)).fetchone()
                    if isolated and isolated["status"] == "failed":
                        raise BackfillError("Invoice 160059 remains isolated; use --continue-pending")
                unauthorized_marker = store.get(PENDING_CONTINUATION_161528_KEY)
                if unauthorized_marker == PENDING_CONTINUATION_161528_MARKER:
                    isolated = store.db.execute(
                        "SELECT status FROM documents WHERE type='invoice' AND zoho_id=?",
                        (PENDING_CONTINUATION_161528_MARKER["document_id"],)).fetchone()
                    if isolated and isolated["status"] == "failed":
                        raise BackfillError("Invoice 161528 remains isolated; use --continue-pending")
                if store.get("halted"):
                    raise BackfillError("Invoice-only run halted; inspect saved evidence before continuing")
            reconcile_interrupted(client, store, schema)
            verified = store.db.execute("SELECT count(*) FROM documents WHERE test_verified=1 AND status='updated_verified'").fetchone()[0]
            if (args.run or continue_pending) and not verified:
                raise BackfillError("One changed invoice must pass --test before continuing")
            if args.test and verified:
                print("Invoice test already passed; no further test write sent.")
                return
            batch_limit = args.limit
            if continue_pending:
                eligible = store.db.execute(
                    "SELECT count(*) FROM documents WHERE status='pending'").fetchone()[0]
            else:
                eligible = store.db.execute(
                    "SELECT count(*) FROM documents "
                    "WHERE status NOT IN ('updated_verified','already_correct','conflict')").fetchone()[0]
            batch_total = min(eligible, batch_limit)
            initial_summary = store.report(folder)
            print(progress_line(0, batch_total, initial_summary), flush=True)
            last_progress_at = 0
            checked = 0
            for row in invoices:
                saved = store.db.execute("SELECT status FROM documents WHERE type='invoice' AND zoho_id=?", (row["zoho_id"],)).fetchone()
                if not should_process_invoice(saved[0], continue_pending):
                    continue
                if checked >= batch_limit:
                    break
                checked += 1
                try:
                    changed = update_one(client, store, row, schema, args.test)
                except BackfillError as error:
                    store.update(row, "failed", str(error))
                    store.halt(str(error))
                    raise
                if checked % 10 == 0 or args.test:
                    progress = store.report(folder)
                    print(progress_line(checked, batch_total, progress), flush=True)
                    print(dumps(progress), flush=True)
                    last_progress_at = checked
                if args.test and changed:
                    break
            summary = store.report(folder)
            if checked != last_progress_at:
                print(progress_line(checked, batch_total, summary), flush=True)
            print(dumps(summary), flush=True)
            if continue_pending:
                isolated_numbers = ["157368"]
                if store.get(PENDING_CONTINUATION_160059_KEY) == PENDING_CONTINUATION_160059_MARKER:
                    isolated_numbers.append("160059")
                if store.get(PENDING_CONTINUATION_161528_KEY) == PENDING_CONTINUATION_161528_MARKER:
                    isolated_numbers.append("161528")
                if store.get(PENDING_CONTINUATION_165534_KEY) == PENDING_CONTINUATION_165534_MARKER:
                    isolated_numbers.append("165534")
                if store.get(PENDING_CONTINUATION_165818_KEY) == PENDING_CONTINUATION_165818_MARKER:
                    isolated_numbers.append("165818")
                if store.get(PENDING_CONTINUATION_167764_KEY) == PENDING_CONTINUATION_167764_MARKER:
                    isolated_numbers.append("167764")
                if store.get(PENDING_CONTINUATION_168436_KEY) == PENDING_CONTINUATION_168436_MARKER:
                    isolated_numbers.append("168436")
                if store.get(PENDING_CONTINUATION_168777_KEY) == PENDING_CONTINUATION_168777_MARKER:
                    isolated_numbers.append("168777")
                if store.get(PENDING_CONTINUATION_168812_KEY) == PENDING_CONTINUATION_168812_MARKER:
                    isolated_numbers.append("168812")
                if store.get(PENDING_CONTINUATION_168905_KEY) == PENDING_CONTINUATION_168905_MARKER:
                    isolated_numbers.append("168905")
                isolated_text = " and ".join(isolated_numbers)
                if summary["pending"]:
                    print("Invoices " + isolated_text + " remain failed and isolated. "
                          "Use --continue-pending for another bounded batch.", flush=True)
                else:
                    print("All pending invoices were processed. Invoices " + isolated_text +
                          " remain failed and isolated; reconcile them before declaring completion.", flush=True)
        finally:
            store.report(folder)
            store.db.close()


if __name__ == "__main__":
    try:
        main()
    except (BackfillError, OSError, ValueError, sqlite3.Error) as error:
        print("Invoice run stopped: " + str(error), file=sys.stderr)
        if getattr(error, "status", None) is not None:
            diagnostic = Path("output/corrected_run/invoice_field_id_run") / ("access_error_" + str(time.time_ns()) + ".json")
            try:
                diagnostic.parent.mkdir(parents=True, exist_ok=True)
                diagnostic.write_text(dumps({"http_status": error.status,
                    "response": getattr(error, "body", None)}) + "\n", encoding="utf-8")
                print("API error evidence: " + str(diagnostic), file=sys.stderr)
            except OSError:
                pass
        sys.exit(1)
