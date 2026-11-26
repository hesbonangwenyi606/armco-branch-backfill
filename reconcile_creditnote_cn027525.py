"""Reconcile the interrupted CN027525 attempt from its saved read-only evidence."""
import hashlib
import json
from pathlib import Path
import sqlite3

from backfill_customer_branch import BackfillError, dumps, load_map, run_lock
from run_creditnote_backfill import ROOT, INPUT, FIELD, progress, verify


def main():
    folder = Path('output/corrected_run/creditnote_preflight_1795439945150337300')
    def load(name):
        return json.loads((folder / (name + '.json')).read_text(encoding='utf-8'))
    rows, fingerprint = load_map('data/Armco_Branch_Backfill_API_Map_corrected.csv')
    with run_lock(ROOT), sqlite3.connect(ROOT / 'progress.sqlite3', timeout=30) as db:
        db.execute('BEGIN IMMEDIATE')
        identity = json.loads(db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()[0])
        preflight = load('identity')
        if (identity['map_sha256'] != fingerprint or identity['organisation'] != '941141787'
                or identity['field'] != FIELD or identity['candidates_sha256'] != hashlib.sha256(INPUT.read_bytes()).hexdigest()
                or preflight != {'map_sha256': fingerprint, 'organisation_id': identity['organisation']}):
            raise BackfillError('Journal or preflight identity differs')
        row = db.execute("SELECT id, status, evidence FROM attempts WHERE number='CN027525'").fetchone()
        if not row or row[1] != 'failed':
            raise BackfillError('Expected attempted row not found')
        doc_id, _, raw = row
        evidence = json.loads(raw)
        cn, inv = evidence['mapping'], evidence['invoice_mapping']
        if evidence.get('error') != 'Protected data changed: /is_inventory_valuation_pending' or 'write_response' not in evidence:
            raise BackfillError('Failure evidence does not match the investigated valuation flag')
        if (cn not in rows or inv not in rows or cn['zoho_id'] != doc_id
                or evidence['request'] != {'invoice_id': inv['zoho_id'], 'custom_fields': [{'customfield_id': FIELD, 'value': cn['branch_record_id']}]}):
            raise BackfillError('Saved mapping or request differs')
        candidate, after, invoice = load('candidate'), load('creditnote'), load('invoice')
        if (candidate['creditnote_id'] != doc_id or candidate['invoice_id'] != inv['zoho_id']
                or after.get('creditnote_id') != doc_id or after.get('creditnote_number') != cn['number']
                or invoice.get('invoice_id') != inv['zoho_id'] or invoice.get('invoice_number') != inv['number']
                or not after.get('customer_id') or after['customer_id'] != invoice.get('customer_id')):
            raise BackfillError('Fresh document identity or customer differs')
        allocations, fields = load('allocations'), load('custom_fields')
        schema = {'field_id': FIELD, 'api_name': 'cf_customer_branch'}
        verify(evidence['before'], after, evidence['allocations_before'], allocations, fields, cn, inv, schema)
        # Save a backup before changing the journal; the transaction is released first.
        db.rollback()
        backup = ROOT / 'before_CN027525_reconciliation.sqlite3'
        if backup.exists():
            raise BackfillError('Reconciliation backup already exists')
        with sqlite3.connect(backup) as dest:
            db.backup(dest)
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT status,evidence FROM attempts WHERE id=?', (doc_id,)).fetchone()
        if current != ('failed', raw):
            raise BackfillError('Journal changed during reconciliation')
        evidence.update({'after': after, 'allocations_after': allocations, 'fields_after': fields,
                         'reconciled_from': str(folder)})
        db.execute("UPDATE attempts SET status='verified', evidence=? WHERE id=?", (dumps(evidence), doc_id))
        db.commit()
        with INPUT.open(encoding='utf-8-sig') as f:
            total = sum(1 for _ in f) - 1
        progress(db, total)
        print('CN027525 reconciled as verified; no Zoho write performed.')


if __name__ == '__main__':
    main()
