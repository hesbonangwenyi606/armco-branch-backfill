"""Resumable field-only backfill; stops on conflicts or uncertain writes."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

from backfill_customer_branch import (BackfillError, Client, branch_value, dumps, load_map,
    run_lock, settings, validate_lookup_schema)
from test_creditnote_branch import FIELD_ID
from test_creditnote_cn027250 import field_value
from test_creditnote_field_only import verify_result

ROOT = Path('output/corrected_run/creditnote_field_only_batch')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=10, help='Maximum new records (default 10)')
    parser.add_argument('--recover-cn029132-read', action='store_true', help='Reconcile the specific pre-write read failure before continuing')
    parser.add_argument('--recover-cn030201-read', action='store_true', help='Reconcile the specific CN030201 pre-write read failure before continuing')
    parser.add_argument('--reconcile-pro000054', action='store_true', help='Verify the recorded PRO000054 write before continuing')
    args = parser.parse_args()
    if not 1 <= args.limit <= 2452:
        raise BackfillError('Limit must be between 1 and 2452')
    rows, fingerprint = load_map('data/Armco_Branch_Backfill_API_Map_corrected.csv')
    notes = [r for r in rows if r['type'] == 'creditnote']
    cfg = settings(['creditnote'])
    if cfg['ZOHO_ORG_ID'] != '941141787' or cfg['CF_CREDITNOTE_ID'] != FIELD_ID:
        raise BackfillError('Unexpected organization or field')
    ROOT.mkdir(parents=True, exist_ok=True)
    with run_lock(ROOT), sqlite3.connect(ROOT / 'progress.sqlite3', timeout=30) as db:
        db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, number TEXT, status TEXT, evidence TEXT)')
        identity = dumps({'map': fingerprint, 'org': cfg['ZOHO_ORG_ID'], 'api': cfg['ZOHO_API'], 'field': FIELD_ID})
        old = db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()
        if old and old[0] != identity:
            raise BackfillError('Journal identity mismatch')
        db.execute("INSERT OR IGNORE INTO meta VALUES ('identity', ?)", (identity,))
        db.commit()
        if args.recover_cn029132_read or args.recover_cn030201_read:
            if args.recover_cn029132_read and args.recover_cn030201_read:
                raise BackfillError('Select only one recovery')
            number_to_recover = 'CN030201' if args.recover_cn030201_read else 'CN029132'
            failures = db.execute("SELECT id, number, status, evidence FROM attempts WHERE status IN ('attempting','failed')").fetchall()
            target = next((r for r in notes if r['number'] == number_to_recover), None)
            if len(failures) != 1 or not target:
                raise BackfillError('Recovery requires exactly the ' + number_to_recover + ' failed attempt')
            ident, number, status, saved = failures[0]
            evidence = json.loads(saved)
            if (ident != target['zoho_id'] or number != target['number'] or status != 'failed' or
                    evidence != {'mapping': target, 'error': 'Read failed after bounded retries'}):
                raise BackfillError('Recovery evidence does not match the pre-write read failure')
            client = Client(cfg, delay=60/90, requests_per_minute=90)
            client.read(target)
            db.execute('CREATE TABLE IF NOT EXISTS recovered_attempts (id TEXT, number TEXT, status TEXT, evidence TEXT, recovered_at TEXT DEFAULT CURRENT_TIMESTAMP)')
            db.execute('INSERT INTO recovered_attempts (id, number, status, evidence) VALUES (?, ?, ?, ?)', failures[0])
            db.execute('DELETE FROM attempts WHERE id=? AND status=? AND evidence=?', (ident, status, saved))
            db.commit()
        if args.reconcile_pro000054:
            failures = db.execute("SELECT id, number, status, evidence FROM attempts WHERE status IN ('attempting','failed')").fetchall()
            target = next((r for r in notes if r['number'] == 'PRO000054'), None)
            if len(failures) != 1 or not target:
                raise BackfillError('Reconciliation requires exactly the PRO000054 failed attempt')
            ident, number, status, saved = failures[0]
            evidence = json.loads(saved)
            expected_request = {'method': 'POST', 'path': '/creditnotes/' + target['zoho_id'] + '/customfields',
                                'payload': {'custom_fields': [{'customfield_id': FIELD_ID, 'value': target['branch_record_id']}]}}
            if (ident != target['zoho_id'] or number != target['number'] or status != 'failed' or
                    evidence.get('mapping') != target or evidence.get('error') != 'Read failed after bounded retries' or
                    evidence.get('request') != expected_request or
                    evidence.get('write_outcome', {}).get('response', {}).get('code') != 0 or
                    not all(k in evidence for k in ('before', 'allocations_before', 'fields_before'))):
                raise BackfillError('PRO000054 evidence does not match the expected post-write failure')
            client = Client(cfg, delay=60/90, requests_per_minute=90)
            schema = validate_lookup_schema(client.request('GET', '/settings/fields', query={'entity': 'creditnote'}), FIELD_ID, 'creditnote')
            after = client.read(target)
            base = '/creditnotes/' + ident
            after_allocations = client.request('GET', base + '/invoices')
            after_fields = client.request('GET', base + '/customfields')
            result = verify_result(evidence['before'], after, evidence['allocations_before'], after_allocations,
                                   evidence['fields_before'], after_fields, target, schema, evidence['write_outcome'])
            if not result['verified']:
                raise BackfillError('PRO000054 readback did not verify; journal remains blocked: ' + dumps(result))
            db.execute('CREATE TABLE IF NOT EXISTS recovered_attempts (id TEXT, number TEXT, status TEXT, evidence TEXT, recovered_at TEXT DEFAULT CURRENT_TIMESTAMP)')
            db.execute('INSERT INTO recovered_attempts (id, number, status, evidence) VALUES (?, ?, ?, ?)', failures[0])
            evidence.update({'after': after, 'allocations_after': after_allocations, 'fields_after': after_fields, 'verification': result})
            evidence.pop('error', None)
            db.execute("UPDATE attempts SET status='verified', evidence=? WHERE id=? AND status='failed' AND evidence=?", (dumps(evidence), ident, saved))
            db.commit()
            print('PRO000054 reconciled and verified.', flush=True)
        if db.execute("SELECT 1 FROM attempts WHERE status IN ('attempting','failed') LIMIT 1").fetchone():
            raise BackfillError('Unresolved attempt; inspect journal before continuing')
        client = Client(cfg, delay=60/90, requests_per_minute=90)
        schema = validate_lookup_schema(client.request('GET', '/settings/fields', query={'entity': 'creditnote'}), FIELD_ID, 'creditnote')
        processed = 0
        for row in notes:
            if processed >= args.limit:
                break
            if db.execute('SELECT 1 FROM attempts WHERE id=?', (row['zoho_id'],)).fetchone():
                continue
            evidence = {'mapping': row}
            try:
                before = client.read(row)
                base = '/creditnotes/' + row['zoho_id']
                allocations = client.request('GET', base + '/invoices')
                fields = client.request('GET', base + '/customfields')
                evidence.update({'before': before, 'allocations_before': allocations, 'fields_before': fields})
                if before.get('creditnote_id') != row['zoho_id'] or before.get('creditnote_number') != row['number'] or not before.get('customer_id'):
                    raise BackfillError('Document identity or customer invalid')
                if not isinstance(allocations.get('invoices_credited'), list):
                    raise BackfillError('Invalid allocation response')
                value, doc_value = field_value(fields), branch_value(before, FIELD_ID, schema)
                if value == row['branch_record_id'] and doc_value == value:
                    for container in (before, before.get('custom_field_hash', {})):
                        if isinstance(container, dict) and (container.get('cf_customer_branch_unformatted') not in (None, '', value) or
                                container.get('cf_customer_branch_secondary_value_formatted') not in (None, '', row['branch_name'])):
                            raise BackfillError('Conflicting branch mirror')
                    status = 'already_present'
                else:
                    if value != '' or doc_value != '' or before.get('status') == 'void':
                        raise BackfillError('Branch populated, ambiguous, or credit note void')
                    for container in (before, before.get('custom_field_hash', {})):
                        if isinstance(container, dict) and any(container.get(k) not in (None, '') for k in ('cf_customer_branch_unformatted', 'cf_customer_branch_secondary_value_formatted')):
                            raise BackfillError('Branch mirror populated')
                    fresh = client.read(row)
                    fresh_alloc = client.request('GET', base + '/invoices')
                    fresh_fields = client.request('GET', base + '/customfields')
                    if before != fresh or allocations != fresh_alloc or fields != fresh_fields:
                        raise BackfillError('Concurrent change detected')
                    payload = {'custom_fields': [{'customfield_id': FIELD_ID, 'value': row['branch_record_id']}]}
                    evidence['request'] = {'method': 'POST', 'path': base + '/customfields', 'payload': payload}
                    db.execute('INSERT INTO attempts VALUES (?, ?, ?, ?)', (row['zoho_id'], row['number'], 'attempting', dumps(evidence)))
                    db.commit()
                    outcome = {'response': client.request('POST', base + '/customfields', payload)}
                    evidence['write_outcome'] = outcome
                    db.execute('UPDATE attempts SET evidence=? WHERE id=?', (dumps(evidence), row['zoho_id']))
                    db.commit()
                    after = client.read(row)
                    after_alloc = client.request('GET', base + '/invoices')
                    after_fields = client.request('GET', base + '/customfields')
                    evidence.update({'after': after, 'allocations_after': after_alloc, 'fields_after': after_fields})
                    result = verify_result(before, after, allocations, after_alloc, fields, after_fields, row, schema, outcome)
                    evidence['verification'] = result
                    if not result['verified']:
                        raise BackfillError('Post-write verification failed')
                    status = 'verified'
                db.execute('INSERT INTO attempts VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,evidence=excluded.evidence',
                           (row['zoho_id'], row['number'], status, dumps(evidence)))
                db.commit()
                processed += 1
                counts = dict(db.execute('SELECT status, COUNT(*) FROM attempts GROUP BY status'))
                print(dumps({'number': row['number'], 'status': status, 'checked_this_run': processed, 'counts': counts, 'total': len(notes)}), flush=True)
            except (BackfillError, OSError, ValueError) as exc:
                evidence['error'] = str(exc)
                if hasattr(exc, 'status'):
                    evidence.update({'http_status': exc.status, 'error_response': exc.body})
                db.execute('INSERT INTO attempts VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,evidence=excluded.evidence',
                           (row['zoho_id'], row['number'], 'failed', dumps(evidence)))
                db.commit()
                raise BackfillError('Stopped at ' + row['number'] + ': ' + str(exc)) from None
        print(dumps({'checked_this_run': processed, 'counts': dict(db.execute('SELECT status, COUNT(*) FROM attempts GROUP BY status')), 'total': len(notes)}))


if __name__ == '__main__':
    try:
        main()
    except (BackfillError, OSError, ValueError, sqlite3.Error) as exc:
        print('Field-only batch stopped: ' + str(exc), file=sys.stderr)
        sys.exit(1)
