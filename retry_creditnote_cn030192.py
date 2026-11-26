"""Guarded single retry of CN030192 after the authorization issue is reviewed."""
import hashlib
import json
import sqlite3
import sys

from backfill_customer_branch import (APIError, BackfillError, Client, branch_value,
    document_differences, dumps, load_map, run_lock, settings, validate_lookup_schema)
from run_creditnote_backfill import FIELD, INPUT, ROOT, normalized, progress, verify
from test_creditnote_cn027250 import field_value


def main():
    rows, fingerprint = load_map('data/Armco_Branch_Backfill_API_Map_corrected.csv')
    cfg = settings(['creditnote', 'invoice'])
    with run_lock(ROOT), sqlite3.connect(ROOT / 'progress.sqlite3', timeout=30) as db:
        identity = json.loads(db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()[0])
        if (identity['map_sha256'] != fingerprint or identity['candidates_sha256'] != hashlib.sha256(INPUT.read_bytes()).hexdigest()
                or identity['organisation'] != cfg['ZOHO_ORG_ID'] or identity['api'] != cfg['ZOHO_API']
                or identity['field'] != FIELD or cfg['CF_CREDITNOTE_ID'] != FIELD):
            raise BackfillError('Journal identity differs')
        row = db.execute("SELECT id,status,evidence FROM attempts WHERE number='CN030192'").fetchone()
        if not row or row[1] != 'isolated':
            raise BackfillError('Expected isolated CN030192 not found')
        doc_id, _, raw = row
        e = json.loads(raw)
        cn, inv = e['mapping'], e['invoice_mapping']
        payload = {'invoice_id': inv['zoho_id'], 'custom_fields': [{'customfield_id': FIELD, 'value': cn['branch_record_id']}]}
        if (e.get('isolation') != 'CN030192_401_code57' or e.get('http_status') != 401
                or e.get('response', {}).get('code') != 57 or e.get('request') != payload
                or cn not in rows or inv not in rows or cn['zoho_id'] != doc_id):
            raise BackfillError('Saved attempt does not match expected rejection')
        if db.execute("SELECT 1 FROM attempts WHERE status IN ('failed','attempting') LIMIT 1").fetchone():
            raise BackfillError('Another unresolved attempt blocks retry')
        client = Client(cfg, delay=60/90, requests_per_minute=90)
        schema = validate_lookup_schema(client.request('GET', '/settings/fields', query={'entity': 'creditnote'}), FIELD, 'creditnote')
        before, invoice = client.read(cn), client.read(inv)
        allocations = client.request('GET', '/creditnotes/' + doc_id + '/invoices')
        fields = client.request('GET', '/creditnotes/' + doc_id + '/customfields')
        credited = allocations.get('invoices_credited')
        if (not before.get('customer_id') or before['customer_id'] != invoice.get('customer_id')
                or allocations != e['allocations_before'] or not isinstance(credited, list) or len(credited) != 1
                or credited[0].get('invoice_id') != inv['zoho_id'] or credited[0].get('invoice_number') != inv['number']
                or field_value(fields) != '' or branch_value(before, FIELD, schema) != ''
                or before.get('invoice_id') not in (None, '') or before.get('invoice_number') not in (None, '')
                or before.get('reference_invoice_ids') not in (None, '', [])
                or document_differences(normalized(e['before'], schema), normalized(before, schema))):
            raise BackfillError('Fresh state differs or is ambiguous; retry blocked')
        for container in (before, before.get('custom_field_hash', {})):
            if isinstance(container, dict) and any(container.get(k) not in (None, '') for k in ('cf_customer_branch_unformatted', 'cf_customer_branch_secondary_value_formatted')):
                raise BackfillError('Branch mirror is populated; retry blocked')
        backup = ROOT / 'before_CN030192_retry.sqlite3'
        if backup.exists():
            raise BackfillError('Retry backup already exists; inspect journal')
        with sqlite3.connect(backup) as dest:
            db.backup(dest)
        evidence = {'previous_attempt': e, 'mapping': cn, 'invoice_mapping': inv, 'before': before,
                    'invoice_before': invoice, 'allocations_before': allocations, 'fields_before': fields, 'request': payload}
        db.execute("UPDATE attempts SET status='attempting', evidence=? WHERE id=? AND status='isolated' AND evidence=?", (dumps(evidence), doc_id, raw))
        if db.total_changes != 1:
            raise BackfillError('Journal changed during retry')
        db.commit()
        try:
            evidence['write_response'] = client.request('PUT', '/creditnotes/' + doc_id, payload)
            db.execute('UPDATE attempts SET evidence=? WHERE id=?', (dumps(evidence), doc_id)); db.commit()
            after = client.read(cn)
            after_allocations = client.request('GET', '/creditnotes/' + doc_id + '/invoices')
            after_fields = client.request('GET', '/creditnotes/' + doc_id + '/customfields')
            evidence.update({'after': after, 'allocations_after': after_allocations, 'fields_after': after_fields})
            verify(before, after, allocations, after_allocations, after_fields, cn, inv, schema)
            db.execute("UPDATE attempts SET status='verified', evidence=? WHERE id=?", (dumps(evidence), doc_id)); db.commit()
            print('CN030192 updated and verified.')
        except (BackfillError, OSError, ValueError) as error:
            evidence['error'] = str(error)
            if isinstance(error, APIError):
                evidence.update({'http_status': error.status, 'response': error.body})
            db.execute("UPDATE attempts SET status='failed', evidence=? WHERE id=?", (dumps(evidence), doc_id)); db.commit()
            raise
        finally:
            with INPUT.open(encoding='utf-8-sig') as f:
                progress(db, sum(1 for _ in f) - 1)


if __name__ == '__main__':
    try:
        main()
    except (BackfillError, OSError, ValueError, sqlite3.Error) as error:
        print('CN030192 retry stopped: ' + str(error), file=sys.stderr)
        sys.exit(1)
