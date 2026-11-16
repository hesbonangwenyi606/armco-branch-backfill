"""Read-only inspection of the documented CN027182 custom-fields endpoint."""
import sys
import time
from pathlib import Path

from backfill_customer_branch import BackfillError, Client, dumps, settings
from test_creditnote_branch import DOCUMENT_ID, FIELD_ID


def field_metadata(value, path=""):
    found = []
    if isinstance(value, dict):
        if any(key in value for key in ("field_id", "customfield_id", "api_name", "index")):
            keys = ("field_id", "customfield_id", "api_name", "index", "data_type", "entity",
                    "related_entity", "is_active", "is_read_only")
            item = {key: value[key] for key in keys if key in value and
                    isinstance(value[key], (str, int, bool, type(None)))}
            found.append({"path": path, **item})
        for key, child in value.items():
            found.extend(field_metadata(child, path + "/" + key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(field_metadata(child, path + "/" + str(index)))
    return found


def main():
    cfg = settings(["creditnote"])
    if cfg["ZOHO_ORG_ID"] != "941141787" or cfg["CF_CREDITNOTE_ID"] != FIELD_ID:
        raise BackfillError("Settings do not match the investigated organisation/field")
    body = Client(cfg).request("GET", "/creditnotes/" + DOCUMENT_ID + "/customfields")
    folder = Path("output/corrected_run") / ("creditnote_endpoint_fields_" + str(time.time_ns()))
    folder.mkdir()
    (folder / "response.json").write_text(dumps(body) + "\n", encoding="utf-8")
    print(dumps({"response_keys": sorted(body), "field_metadata": field_metadata(body),
                 "evidence_folder": str(folder), "remote_updates_performed": 0}))


if __name__ == "__main__":
    try:
        main()
    except (BackfillError, OSError) as error:
        print("Inspection stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
