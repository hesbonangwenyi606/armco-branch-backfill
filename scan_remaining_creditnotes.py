"""Resumable, read-only classification of mapped credit notes outside the batch."""
import argparse
import csv
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from backfill_customer_branch import (BackfillError, branch_value, dumps, load_map,
                                      run_lock, settings, validate_lookup_schema)
from find_creditnote_candidate import ReadOnlyClient
from run_creditnote_backfill import FIELD, INPUT
from test_creditnote_cn027250 import field_value

ROOT = Path('output/corrected_run/creditnote_remaining_scan')


def classify(cn, doc, fields, allocations, invoices, client, schema):
    if doc.get('status') == 'void':
        return 'void', None
    value = field_value(fields)
    doc_value = branch_value(doc, FIELD, schema)
    if value or doc_value:
        return ('branch_matches' if value == cn['branch_record_id'] and doc_value == cn['branch_record_id']
                else 'branch_present_or_ambiguous'), None
    for container in (doc, doc.get('custom_field_hash', {})):
        if isinstance(container, dict) and any(container.get(k) not in (None, '') for k in
                ('cf_customer_branch_unformatted', 'cf_customer_branch_secondary_value_formatted')):
            return 'branch_present_or_ambiguous', None
    credited = allocations.get('invoices_credited')
    if not isinstance(credited, list):
        return 'allocation_response_invalid', None
    if len(credited) != 1:
        return ('no_allocation' if not credited else 'multiple_allocations'), None
    allocation = credited[0]
    inv = invoices.get(str(allocation.get('invoice_id', '')))
    if not inv or allocation.get('invoice_number') != inv['number']:
        return 'allocation_not_in_approved_map', None
    if inv['branch_record_id'] != cn['branch_record_id'] or inv['branch_name'] != cn['branch_name']:
        return 'invoice_mapping_branch_differs', None
    invoice = client.read(inv)
    if not doc.get('customer_id') or doc['customer_id'] != invoice.get('customer_id'):
        return 'customer_mismatch', None
    if (doc.get('invoice_id') not in (None, '') or doc.get('invoice_number') not in (None, '')
            or doc.get('reference_invoice_ids') not in (None, '', [])):
        return 'existing_association_review', None
    return 'potential_candidate', inv


def export(db, total):
    records = db.execute('SELECT number,status,invoice_number FROM results ORDER BY rowid').fetchall()
    with (ROOT / 'report.csv').open('w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['creditnote_number', 'classification', 'invoice_number'])
        writer.writerows(records)
    counts = Counter(r[1] for r in records)
    (ROOT / 'progress.md').write_text('# Remaining credit-note scan\n\n'
        + f'Checked: {len(records)} / {total}\n\n'
        + '\n'.join(f'- {k}: {v}' for k, v in sorted(counts.items()))
        + '\n\nRead-only classification; potential candidates require independent review before any updates.\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=100, help='Maximum new notes to inspect (1–1047)')
    args = parser.parse_args()
    if not 1 <= args.limit <= 1047:
        raise BackfillError('Limit must be between 1 and 1047')
    rows, fingerprint = load_map('data/Armco_Branch_Backfill_API_Map_corrected.csv')
    raw = INPUT.read_bytes()
    with INPUT.open(encoding='utf-8-sig', newline='') as f:
        excluded = {r['creditnote_id'] for r in csv.DictReader(f)}
    remaining = [r for r in rows if r['type'] == 'creditnote' and r['zoho_id'] not in excluded]
    invoices = {r['zoho_id']: r for r in rows if r['type'] == 'invoice'}
    cfg = settings(['creditnote', 'invoice'])
    if cfg['ZOHO_ORG_ID'] != '941141787' or cfg['CF_CREDITNOTE_ID'] != FIELD:
        raise BackfillError('Organisation or field differs from approved configuration')
    ROOT.mkdir(parents=True, exist_ok=True)
    with run_lock(ROOT), sqlite3.connect(ROOT / 'scan.sqlite3', timeout=30) as db:
        db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS results (id TEXT PRIMARY KEY, number TEXT, status TEXT, invoice_number TEXT)')
        identity = dumps({'map': fingerprint, 'candidates': hashlib.sha256(raw).hexdigest(),
                          'org': cfg['ZOHO_ORG_ID'], 'api': cfg['ZOHO_API'], 'field': FIELD})
        existing = db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()
        if existing and existing[0] != identity:
            raise BackfillError('Scan identity differs from current configuration')
        db.execute("INSERT OR IGNORE INTO meta VALUES ('identity',?)", (identity,))
        db.commit()
        client = ReadOnlyClient(cfg, delay=60/90, requests_per_minute=90)
        schema = validate_lookup_schema(client.request('GET', '/settings/fields', query={'entity': 'creditnote'}), FIELD, 'creditnote')
        checked = 0
        export(db, len(remaining))
        for cn in remaining:
            if checked >= args.limit:
                break
            if db.execute('SELECT 1 FROM results WHERE id=?', (cn['zoho_id'],)).fetchone():
                continue
            doc = client.read(cn)
            fields = client.request('GET', '/creditnotes/' + cn['zoho_id'] + '/customfields')
            allocations = client.request('GET', '/creditnotes/' + cn['zoho_id'] + '/invoices')
            classification, inv = classify(cn, doc, fields, allocations, invoices, client, schema)
            db.execute('INSERT INTO results VALUES (?,?,?,?)', (cn['zoho_id'], cn['number'], classification, inv['number'] if inv else ''))
            db.commit()
            checked += 1
            export(db, len(remaining))
            print(json.dumps({'checked_in_run': checked, 'number': cn['number'], 'classification': classification}), flush=True)
        print(json.dumps({'checked_in_run': checked, 'report': str(ROOT / 'report.csv'), 'remote_updates_performed': 0}), flush=True)


if __name__ == '__main__':
    try:
        main()
    except (BackfillError, OSError, ValueError, sqlite3.Error) as error:
        print('Read-only scan stopped: ' + str(error), file=sys.stderr)
        sys.exit(1)
