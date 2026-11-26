"""Read-only comparison of one invoice's live branch with the approved map."""
import json
import sys
from backfill_customer_branch import BackfillError, Client, branch_value, load_map, settings, validate_lookup_schema
from run_invoice_backfill import lookup, branch_is_omitted


def main():
    if len(sys.argv) != 2:
        raise BackfillError('Enter one invoice number')
    rows, _ = load_map('data/Armco_Branch_Backfill_API_Map_corrected.csv')
    matches = [r for r in rows if r['type'] == 'invoice' and r['number'] == sys.argv[1]]
    if len(matches) != 1:
        raise BackfillError('Invoice number is missing or ambiguous in the approved map')
    row = matches[0]
    cfg = settings(['invoice'])
    if cfg['ZOHO_ORG_ID'] != '941141787' or cfg['CF_INVOICE_ID'] != '1462401000020819910':
        raise BackfillError('Organisation or invoice field differs from the approved configuration')
    client = Client(cfg)
    schema = validate_lookup_schema(client.request('GET', '/settings/fields', query={'entity': 'invoice'}), cfg['CF_INVOICE_ID'], 'invoice')
    doc = client.read(row)
    current, body = lookup(client, row, cfg['CF_INVOICE_ID'])
    if not branch_is_omitted(doc, cfg['CF_INVOICE_ID'], schema):
        if branch_value(doc, cfg['CF_INVOICE_ID'], schema) != current:
            raise BackfillError('Invoice and custom-fields endpoint disagree about the branch')
    field = next(f for f in body['custom_fields'] if str(f.get('customfield_id')) == cfg['CF_INVOICE_ID'])
    print(json.dumps({'invoice_number': row['number'], 'expected_branch': row['branch_name'],
        'actual_branch': field.get('value_formatted') or ('Blank' if not current else 'ID ' + current),
        'matches': current == row['branch_record_id'], 'remote_updates_performed': 0}))


if __name__ == '__main__':
    try:
        main()
    except (BackfillError, OSError, ValueError) as exc:
        print('Invoice check stopped: ' + str(exc), file=sys.stderr)
        sys.exit(1)
