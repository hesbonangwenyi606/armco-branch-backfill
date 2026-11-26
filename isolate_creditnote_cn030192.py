"""Preserve the rejected CN030192 attempt and permit other candidates to proceed."""
import hashlib
import json
import sqlite3
from pathlib import Path

from backfill_customer_branch import BackfillError, branch_value, document_differences, dumps, load_map, run_lock
from run_creditnote_backfill import FIELD, INPUT, ROOT, normalized, progress
from test_creditnote_cn027250 import field_value


def main():
    folder = Path('output/corrected_run/creditnote_preflight_1795492118697383700')
    def load(name):
        return json.loads((folder / (name + '.json')).read_text(encoding='utf-8'))
    rows, fingerprint = load_map('data/Armco_Branch_Backfill_API_Map_corrected.csv')
    with run_lock(ROOT), sqlite3.connect(ROOT / 'progress.sqlite3', timeout=30) as db:
        db.execute('BEGIN IMMEDIATE')
        identity = json.loads(db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()[0])
        if (identity['map_sha256'] != fingerprint or identity['organisation'] != '941141787'
                or identity['field'] != FIELD or identity['candidates_sha256'] != hashlib.sha256(INPUT.read_bytes()).hexdigest()
                or load('identity') != {'map_sha256': fingerprint, 'organisation_id': identity['organisation']}):
            raise BackfillError('Journal or preflight identity differs')
        row = db.execute("SELECT id,status,evidence FROM attempts WHERE number='CN030192'").fetchone()
        if not row or row[1] != 'failed':
            raise BackfillError('Expected failed CN030192 not found')
        doc_id, _, raw = row
        e = json.loads(raw)
        cn, inv = e['mapping'], e['invoice_mapping']
        if (e.get('http_status') != 401 or e.get('response', {}).get('code') != 57
                or 'write_response' in e or cn not in rows or inv not in rows or cn['zoho_id'] != doc_id
                or e.get('request') != {'invoice_id': inv['zoho_id'], 'custom_fields': [{'customfield_id': FIELD, 'value': cn['branch_record_id']}]}):
            raise BackfillError('Saved rejection, mapping or request differs')
        candidate, after, invoice = load('candidate'), load('creditnote'), load('invoice')
        schema = {'field_id': FIELD, 'api_name': 'cf_customer_branch'}
        if (candidate['creditnote_id'] != doc_id or candidate['invoice_id'] != inv['zoho_id']
                or after.get('creditnote_id') != doc_id or after.get('creditnote_number') != cn['number']
                or invoice.get('invoice_id') != inv['zoho_id'] or invoice.get('invoice_number') != inv['number']
                or not after.get('customer_id') or after['customer_id'] != invoice.get('customer_id')
                or field_value(load('custom_fields')) != '' or branch_value(after, FIELD, schema) != ''
                or after.get('invoice_id') not in (None, '') or after.get('invoice_number') not in (None, '')
                or after.get('reference_invoice_ids') not in (None, '', [])
                or e['allocations_before'] != load('allocations')
                or document_differences(normalized(e['before'], schema), normalized(after, schema))):
            raise BackfillError('Fresh evidence differs or is ambiguous')
        db.rollback()
        backup = ROOT / 'before_CN030192_isolation.sqlite3'
        if backup.exists():
            raise BackfillError('Isolation backup already exists')
        with sqlite3.connect(backup) as dest:
            db.backup(dest)
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT status,evidence FROM attempts WHERE id=?', (doc_id,)).fetchone() != ('failed', raw):
            raise BackfillError('Journal changed during isolation')
        e.update({'isolation': 'CN030192_401_code57', 'isolation_preflight': str(folder)})
        db.execute("UPDATE attempts SET status='isolated', evidence=? WHERE id=?", (dumps(e), doc_id))
        db.commit()
        with INPUT.open(encoding='utf-8-sig') as f:
            total = sum(1 for _ in f) - 1
        progress(db, total)
        print('CN030192 remains unresolved and is isolated; no Zoho write performed.')


if __name__ == '__main__':
    main()
