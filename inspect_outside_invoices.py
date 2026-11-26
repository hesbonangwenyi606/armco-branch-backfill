"""Inspect outside-map invoice allocations without changing Zoho records."""
import csv
import json
import sys
import time
from pathlib import Path
from backfill_customer_branch import BackfillError, dumps, load_map, numeric, settings
from find_creditnote_candidate import ReadOnlyClient
from run_creditnote_backfill import FIELD


def main():
    rows, fingerprint = load_map('data/Armco_Branch_Backfill_API_Map_corrected.csv')
    credits = {r['number']: r for r in rows if r['type'] == 'creditnote'}
    invoices = {r['zoho_id']: r for r in rows if r['type'] == 'invoice'}
    cfg = settings(['creditnote', 'invoice'])
    if cfg['ZOHO_ORG_ID'] != '941141787' or cfg['CF_CREDITNOTE_ID'] != FIELD:
        raise BackfillError('Unexpected organisation or field')
    with open('output/corrected_run/creditnote_remaining_scan/report.csv', encoding='utf-8-sig', newline='') as f:
        report = list(csv.DictReader(f))
    groups = ('multiple_allocations', 'allocation_not_in_approved_map')
    selected = [r for group in groups for r in [x for x in report if x['classification'] == group][:10]]
    client = ReadOnlyClient(cfg, delay=60/90, requests_per_minute=90)
    folder = Path('output/corrected_run') / ('creditnote_outside_invoice_review_' + str(time.time_ns()))
    folder.mkdir(parents=True)
    (folder / 'identity.json').write_text(dumps({'map_sha256': fingerprint, 'organisation': cfg['ZOHO_ORG_ID']}) + '\n', encoding='utf-8')
    results = []
    for item in selected:
        cn = credits[item['creditnote_number']]
        doc = client.read(cn)
        allocations = client.request('GET', '/creditnotes/' + cn['zoho_id'] + '/invoices').get('invoices_credited')
        if not isinstance(allocations, list):
            raise BackfillError('Invalid allocations for ' + cn['number'])
        outside = []
        for allocation in allocations:
            inv_id, number = str(allocation.get('invoice_id', '')), allocation.get('invoice_number')
            if inv_id in invoices and invoices[inv_id]['number'] == number:
                continue
            if not numeric(inv_id) or not isinstance(number, str) or not number:
                raise BackfillError('Invalid invoice identity for ' + cn['number'])
            invoice = client.read({'type': 'invoice', 'zoho_id': inv_id, 'number': number})
            outside.append({'invoice_number': number, 'status': invoice.get('status'),
                            'same_customer': bool(doc.get('customer_id')) and doc['customer_id'] == invoice.get('customer_id')})
        result = {'creditnote_number': cn['number'], 'classification': item['classification'],
                  'allocation_count': len(allocations), 'outside_invoices': outside}
        results.append(result)
        (folder / 'results.json').write_text(dumps(results) + '\n', encoding='utf-8')
        print(json.dumps(result), flush=True)
    print(json.dumps({'checked': len(results), 'evidence_folder': str(folder), 'remote_updates_performed': 0}), flush=True)


if __name__ == '__main__':
    try:
        main()
    except (BackfillError, OSError, ValueError, KeyError) as error:
        print('Outside-invoice review stopped: ' + str(error), file=sys.stderr)
        sys.exit(1)
