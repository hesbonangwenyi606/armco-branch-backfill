"""Read Cash Customer field metadata; never change invoices or validation rules."""
import argparse
import json
from pathlib import Path
import sys
import time

from backfill_customer_branch import APIError, BackfillError, settings
from reconcile_map import ReadOnlyClient


def summarize(body):
    fields = body.get("fields")
    if not isinstance(fields, list) or any(not isinstance(field, dict) for field in fields):
        raise BackfillError("Unexpected field-settings response; expected a fields list")
    matches = []
    for field in fields:
        labels = [field.get(key, "") for key in ("label", "field_name", "field_name_formatted")]
        if field.get("api_name") == "cf_cash_customer" or any(
                isinstance(label, str) and " ".join(label.split()).casefold() == "cash customer"
                for label in labels):
            matches.append(field)
    if len(matches) != 1:
        raise BackfillError("Cash Customer field was not identified uniquely in invoice metadata")
    field = matches[0]
    display_keys = ("field_id", "customfield_id", "api_name", "label", "field_name",
                    "field_name_formatted", "data_type", "entity", "is_active", "status",
                    "is_mandatory", "is_mandatory_in_hp", "is_read_only", "permission",
                    "visibility", "is_configure_permission")
    rule_keys = [key for key in field if any(term in key.lower() for term in
                 ("validat", "rule", "formula", "condition"))]
    return {"field": {key: field[key] for key in display_keys if key in field
                      and isinstance(field[key], (str, int, float, bool, type(None)))},
            "validation_metadata_keys": sorted(rule_keys),
            "all_metadata_keys": sorted(field),
            "limitation": "Field settings do not establish the complete conditions of a validation rule. An optional field can still be required by a conditional rule."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("output/cash_customer_inspection"))
    parser.add_argument("--probe-validation-rules", action="store_true",
                        help="Read-only probe of an undocumented possible rules endpoint; never modifies rules")
    args = parser.parse_args(argv)
    config = settings([], fields=False)
    query = {"entity": "invoice", "filter_custom_fields": "true"}
    client = ReadOnlyClient(config)
    body = client.request("GET", "/settings/fields", query=query)
    result = summarize(body)
    destination = args.output / ("read_" + str(time.time_ns()))
    destination.mkdir(parents=True)
    # Full metadata stays local, including defaults or validation expressions.
    (destination / "field_settings.json").write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    result.update(evidence_folder=str(destination),
                  organisation_id=config["ZOHO_ORG_ID"],
                  checked_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  remote_updates_performed=0)
    if args.probe_validation_rules:
        # This Books route is not established by public documentation. Probe GET
        # only; an unsupported route or scope response is not proof no rule exists.
        probe = {"method": "GET", "path": "/settings/validationrules",
                 "query": {"entity": "invoice"}, "documented_endpoint": False}
        try:
            rules_body = client.request("GET", probe["path"], query=probe["query"])
        except APIError as error:
            (destination / "rules_response.json").write_text(json.dumps(error.body, indent=2) + "\n", encoding="utf-8")
            probe.update(status="unavailable", http_status=error.status,
                         api_code=error.body.get("code") if isinstance(error.body, dict) else None)
        except BackfillError as error:
            probe.update(status="unavailable", error=str(error))
        else:
            (destination / "rules_response.json").write_text(json.dumps(rules_body, indent=2) + "\n", encoding="utf-8")
            probe.update(status="read_succeeded", response_keys=sorted(rules_body))
            rules = next((rules_body[key] for key in ("validation_rules", "validationrules", "rules")
                          if isinstance(rules_body.get(key), list)), None)
            if rules is not None:
                probe["rules_count"] = len(rules)
                probe["cash_customer_candidates"] = [
                    {key: rule[key] for key in ("id", "rule_id", "validation_rule_id", "name", "rule_name", "status", "field_id")
                     if key in rule and isinstance(rule[key], (str, int, bool))}
                    for rule in rules if isinstance(rule, dict) and any(
                        term in json.dumps(rule).casefold() for term in ("cash customer", "cf_cash_customer", "1462401000020576042"))]
            else:
                probe["status"] = "unconfirmed_response_shape"
        result["validation_rules_probe"] = probe
    (destination / "summary.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (BackfillError, OSError) as error:
        print("Inspection stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
