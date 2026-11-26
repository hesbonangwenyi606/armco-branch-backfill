"""One guarded, experimental field-ID POST for CN027182; never retries a write."""
import json
import sys
from pathlib import Path

from backfill_customer_branch import (BackfillError, Client, branch_value, document_differences,
    dumps, load_map, run_lock, settings, validate_lookup_schema)
from test_creditnote_branch import DOCUMENT_ID, FIELD_ID, NUMBER, projection, save
from test_creditnote_cn027250 import field_value


def verify_result(before, after, allocations, after_allocations, fields, after_fields, row, schema, outcome):
    """Verify the target mirrors explicitly and compare all other returned data."""
    before_projection = projection(before, schema)
    after_projection = projection(after, schema)
    for source, expected in ((before_projection, None), (after_projection, row['branch_name'])):
        for container in (source, source.get('custom_field_hash', {})):
            if isinstance(container, dict):
                name = container.pop('cf_customer_branch_secondary_value_formatted', None)
                if name not in ((None, '') if expected is None else (None, '', expected)):
                    raise BackfillError('Conflicting formatted branch name')
    changes = document_differences(before_projection, after_projection)
    def other_fields(response):
        if not isinstance(response.get('custom_fields'), list):
            raise BackfillError('Invalid custom-fields response')
        return {**response, 'custom_fields': [f for f in response['custom_fields']
                if str(f.get('customfield_id')) != FIELD_ID]}
    field_changes = document_differences(other_fields(fields), other_fields(after_fields))
    try:
        branch_ok = field_value(after_fields) == row['branch_record_id'] and branch_value(after, FIELD_ID, schema) == row['branch_record_id']
    except BackfillError:
        branch_ok = False
    return {'verified': not changes and not field_changes and allocations == after_allocations and branch_ok and 'error' not in outcome,
            'protected_changed_paths': [x['path'] for x in changes],
            'other_custom_field_changed_paths': [x['path'] for x in field_changes],
            'allocations_unchanged': allocations == after_allocations, 'branch_matches': branch_ok}


def main():
    root = Path('output/corrected_run')
    folder = root / 'creditnote_CN027182_field_only_id_experiment'
    rows, _ = load_map('data/Armco_Branch_Backfill_API_Map_corrected.csv')
    row = next(r for r in rows if r['type'] == 'creditnote' and r['zoho_id'] == DOCUMENT_ID and r['number'] == NUMBER)
    cfg = settings(['creditnote'])
    if cfg['ZOHO_ORG_ID'] != '941141787' or cfg['CF_CREDITNOTE_ID'] != FIELD_ID:
        raise BackfillError('Unexpected organization or field')
    with run_lock(root):
        folder.mkdir(exist_ok=True)
        if (folder / 'attempt.json').exists() or (folder / 'before.json').exists():
            raise BackfillError('Existing evidence found; inspect it before any further attempt')
        client = Client(cfg)
        schema = validate_lookup_schema(client.request('GET', '/settings/fields', query={'entity': 'creditnote'}), FIELD_ID, 'creditnote')
        before = client.read(row)
        allocations = client.request('GET', '/creditnotes/' + DOCUMENT_ID + '/invoices')
        fields = client.request('GET', '/creditnotes/' + DOCUMENT_ID + '/customfields')
        if before.get('creditnote_id') != DOCUMENT_ID or before.get('creditnote_number') != NUMBER:
            raise BackfillError('Unexpected document identity')
        if not isinstance(allocations.get('invoices_credited'), list) or field_value(fields) != '' or branch_value(before, FIELD_ID, schema) != '':
            raise BackfillError('Allocation response invalid or branch not blank')
        save(folder, 'before', before)
        save(folder, 'allocations_before', allocations)
        save(folder, 'fields_before', fields)
        save(folder, 'mapping', row)
        fresh = client.read(row)
        fresh_allocations = client.request('GET', '/creditnotes/' + DOCUMENT_ID + '/invoices')
        fresh_fields = client.request('GET', '/creditnotes/' + DOCUMENT_ID + '/customfields')
        if before != fresh or allocations != fresh_allocations or fields != fresh_fields:
            raise BackfillError('Document, allocations or fields changed during preflight')
        path = '/creditnotes/' + DOCUMENT_ID + '/customfields'
        payload = {'custom_fields': [{'customfield_id': FIELD_ID, 'value': row['branch_record_id']}]}
        save(folder, 'attempt', {'method': 'POST', 'path': path, 'payload': payload})
        try:
            outcome = {'response': client.request('POST', path, payload)}
        except BackfillError as error:
            outcome = {'error': str(error), 'http_status': getattr(error, 'status', None), 'response': getattr(error, 'body', None)}
        save(folder, 'write_outcome', outcome)
        after = client.read(row)
        after_allocations = client.request('GET', path.replace('/customfields', '/invoices'))
        after_fields = client.request('GET', path)
        save(folder, 'after', after)
        save(folder, 'allocations_after', after_allocations)
        save(folder, 'fields_after', after_fields)
        result = verify_result(before, after, allocations, after_allocations, fields, after_fields, row, schema, outcome)
        result.update({'http_status': outcome.get('http_status'),
                       'zoho_code': outcome.get('response', {}).get('code') if isinstance(outcome.get('response'), dict) else None,
                       'evidence_folder': str(folder)})
        save(folder, 'summary', result)
        print(dumps(result))
        if not result['verified']:
            raise BackfillError('Experiment not verified; do not repeat without investigation')


if __name__ == '__main__':
    try:
        main()
    except (BackfillError, OSError, ValueError, KeyError, StopIteration) as error:
        print('Experiment stopped: ' + str(error), file=sys.stderr)
        sys.exit(1)
