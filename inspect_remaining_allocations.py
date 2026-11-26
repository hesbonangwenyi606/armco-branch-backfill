"""Read-only investigation of allocation patterns for remaining credit notes."""
import argparse
import csv
import json
import sys
import time
from collections import Counter
from pathlib import Path

from backfill_customer_branch import BackfillError, dumps, load_map, settings
from find_creditnote_candidate import ReadOnlyClient
from run_creditnote_backfill import FIELD

REPORT = Path('output/corrected_run/creditnote_remaining_scan/report.csv')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--per-group', type=int, default=10)
    args = parser.parse_args()
    if not 1 <= args.per_group <= 30:
        raise BackfillError('Use --per-group between 1 and 30')
    rows, fingerprint = load_map('data/Armco_Branch_Backfill_API_Map_corrected.csv')
    credit = {r['number']: r for r in rows if r['type'] == 'creditnote'}
    invoices = {r['zoho_id']: r for r in rows if r['type'] == 'invoice'}
    cfg = settings(['creditnote', 'invoice'])
    if cfg['ZOHO_ORG_ID'] != '941141787' or cfg['CF_CREDITNOTE_ID'] != FIELD:
        raise BackfillError('Organisation or credit-note field differs')
    with REPORT.open(encoding='utf-8-sig', newline='') as f:
        report = list(csv.DictReader(f))
    groups = ('multiple_allocations', 'no_allocation', 'allocation_not_in_approved_map')
    selected = [r for group in groups for r in [x for x in report if x['classification'] == group][:args.per_group]]
    client = ReadOnlyClient(cfg, delay=60/90, requests_per_minute=90)
    folder = Path('output/corrected_run') / ('creditnote_allocation_review_' + str(time.time_ns()))
    folder.mkdir(parents=True)
    (folder / 'identity.json').write_text(dumps({'map_sha256': fingerprint, 'organisation': cfg['ZOHO_ORG_ID']}) + '\n', encoding='utf-8')
    results = []
    for item in selected:
        cn = credit.get(item['creditnote_number'])
        if not cn:
            raise BackfillError('Report contains a credit note outside the approved map')
        doc = client.read(cn)
        allocation = client.request('GET', '/creditnotes/' + cn['zoho_id'] + '/invoices')
        entries = allocation.get('invoices_credited')
        if not isinstance(entries, list):
            raise BackfillError('Invalid allocation response for ' + cn['number'])
        match = Counter()
        for entry in entries:
            mapped = invoices.get(str(entry.get('invoice_id', '')))
            if not mapped or mapped['number'] != entry.get('invoice_number'):
                match['outside_invoice_map'] += 1
            elif mapped['branch_record_id'] == cn['branch_record_id'] and mapped['branch_name'] == cn['branch_name']:
                match['same_mapped_branch'] += 1
            else:
                match['different_mapped_branch'] += 1
        result = {'number': cn['number'], 'previous_classification': item['classification'],
                  'allocation_count': len(entries), 'allocation_mapping': dict(match),
                  'direct_association_present': bool(doc.get('invoice_id') or doc.get('invoice_number'))}
        results.append(result)
        (folder / 'results.json').write_text(dumps(results) + '\n', encoding='utf-8')
        print(json.dumps(result), flush=True)
    print(json.dumps({'checked': len(results), 'evidence_folder': str(folder), 'remote_updates_performed': 0}), flush=True)


if __name__ == '__main__':
    try:
        main()
    except (BackfillError, OSError, ValueError) as error:
        print('Allocation review stopped: ' + str(error), file=sys.stderr)
        sys.exit(1)
