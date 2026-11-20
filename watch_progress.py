"""Monitor an existing local backfill database without calling Zoho."""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time

from backfill_customer_branch import BackfillError, report_rows, run_lock


def snapshot(database, report_folder=None):
    if not database.is_file():
        raise BackfillError("Progress database does not exist")
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        rows = list(db.execute("SELECT * FROM documents ORDER BY type,zoho_id"))
        halted = db.execute("SELECT value FROM meta WHERE key='halted'").fetchone()
    counts = dict(Counter(row["status"] for row in rows))
    # Release the DB read lock before writing reports, keeping the writer moving.
    if report_folder is not None:
        report_rows(rows, report_folder)
    total = sum(counts.values())
    remaining = counts.get("pending", 0) + counts.get("updating", 0)
    reviewed = total - remaining
    return {"checked_utc": datetime.now(timezone.utc).isoformat(), "total": total,
            "reviewed": reviewed, "remaining": remaining,
            "percent_reviewed": round(100 * reviewed / total, 1) if total else 0,
            "provisional_omitted": counts.get("ready_omitted", 0),
            "updated_verified": counts.get("updated_verified", 0),
            "failed": counts.get("failed", 0), "conflict": counts.get("conflict", 0),
            "halted": bool(halted and json.loads(halted[0])), "statuses": counts}


def publish(folder, state):
    if state.get("live"):
        text = ("# Invoice update progress\n\n"
                f"Checked (UTC): {state['checked_utc']}\n\n"
                f"Completed: **{state['completed']:,} / {state['total']:,} ({state['percent_completed']}%)**\n\n"
                f"Updated and verified: **{state['updated_verified']:,}**\n\n"
                f"Already correct: {state['statuses'].get('already_correct', 0):,}\n\n"
                f"Remaining to process: {state['remaining']:,}\n\n"
                f"New verified updates since previous check: **{state['new_verified_updates']}**\n\n"
                f"Measurement interval: {state['measurement_seconds']} seconds\n\n"
                f"Failures: {state['failed']:,}; conflicts: {state['conflict']:,}\n\n"
                f"Monitor status: {state['monitor_status']}\n\n"
                "Refreshes automatically every five minutes. The first check establishes the baseline.\n\n"
                "Automatic reports: [report.csv](report.csv) · [summary.json](summary.json)\n\n"
                "This monitor reads local progress only and makes no Zoho requests.\n")
    else:
        text = ("# Dry-run progress\n\n"
            f"Checked (UTC): {state['checked_utc']}\n\n"
            f"Reviewed: **{state['reviewed']:,} / {state['total']:,} ({state['percent_reviewed']}%)**\n\n"
            f"Remaining: {state['remaining']:,}\n\n"
            f"Provisional omitted lookups: {state['provisional_omitted']:,}\n\n"
            f"Failures: {state['failed']:,}; conflicts: {state['conflict']:,}\n\n"
            f"Updated and verified: {state['updated_verified']:,}\n\n"
            f"Monitor status: {state['monitor_status']}\n\n"
            "Automatic reports: [report.csv](report.csv) · [summary.json](summary.json)\n\n"
            "Reviewed/provisional rows are not completed updates. This monitor makes no Zoho requests.\n")
    # Separate artifacts: never replace the backfill's own reports or progress DB.
    (folder / "progress.md").write_text(text, encoding="utf-8")
    (folder / "progress.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    with (folder / "history.jsonl").open("a", encoding="utf-8") as file:
        file.write(json.dumps(state) + "\n")


def should_stop(state, once, keep_running_on_halt):
    if once or state["remaining"] == 0:
        return True
    return (state["halted"] or state["failed"]) and not keep_running_on_halt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("output/corrected_run"))
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--live", action="store_true", help="Report verified live invoice completions")
    parser.add_argument("--keep-running-on-halt", action="store_true",
                        help="Continue read-only updates when failures or a halt are recorded")
    args = parser.parse_args(argv)
    if args.interval < 1:
        parser.error("interval must be positive")
    database = args.output / "progress.sqlite3"
    if not database.is_file():
        raise BackfillError("Start the backfill/dry run before starting the monitor")
    folder = args.output / "monitor"
    folder.mkdir(exist_ok=True)
    previous, unchanged, previous_verified, previous_time = None, 0, None, None
    with run_lock(folder):
        while True:
            try:
                state = snapshot(database, report_folder=folder)
            except sqlite3.OperationalError:
                # A temporarily busy DB must not stop monitoring or block the writer.
                if args.once:
                    raise
                time.sleep(min(args.interval, 10))
                continue
            signature = state["statuses"]
            unchanged = unchanged + 1 if signature == previous else 0
            previous = signature
            now = time.monotonic()
            if args.live:
                state["live"] = True
                state["completed"] = state["updated_verified"] + signature.get("already_correct", 0)
                state["percent_completed"] = round(100 * state["completed"] / state["total"], 2) if state["total"] else 0
                state["new_verified_updates"] = (state["updated_verified"] - previous_verified
                                                 if previous_verified is not None else 0)
                state["measurement_seconds"] = round(now - previous_time) if previous_time is not None else 0
                previous_verified, previous_time = state["updated_verified"], now
            if state["halted"] or state["failed"]:
                state["monitor_status"] = (
                    "Failures or halt recorded; read-only monitoring continues"
                    if args.keep_running_on_halt else
                    "Failures or halt recorded; check the run terminal")
            elif state["remaining"] == 0:
                state["monitor_status"] = ("Processing finished; review failures/conflicts and final report" if args.live
                                           else "All rows reviewed; reports and live verification still required")
            elif unchanged >= 2:
                state["monitor_status"] = "No progress in two intervals; check terminal/network/quota"
            else:
                state["monitor_status"] = "Monitoring saved progress every %s seconds" % args.interval
            publish(folder, state)
            print(f"{state['checked_utc']} {state['reviewed']}/{state['total']} "
                  f"({state['percent_reviewed']}%); {state['monitor_status']}", flush=True)
            if should_stop(state, args.once, args.keep_running_on_halt):
                return
            time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except (BackfillError, OSError, sqlite3.Error) as error:
        raise SystemExit("Monitor stopped: " + str(error))
    except KeyboardInterrupt:
        raise SystemExit(0)
