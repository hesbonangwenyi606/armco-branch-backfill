import json
from pathlib import Path
import unittest

from backfill_customer_branch import BackfillError
from run_creditnote_backfill import verify


class BatchVerificationTests(unittest.TestCase):
    def test_real_test_snapshot_and_reject_changed_allocation(self):
        folder = Path('output/corrected_run/creditnote_CN027250_guarded_test')
        if not folder.exists():
            self.skipTest('Local live evidence is unavailable')
        def read(name):
            return json.loads((folder / (name + '.json')).read_text(encoding='utf-8'))
        before, after = read('before'), read('after')
        allocations, later = read('allocations_before'), read('allocations_after')
        fields = read('fields_after')
        cn = read('identity')['mapping']
        inv = {'zoho_id': '1462401000001981703', 'number': '152595'}
        schema = {'api_name': 'cf_customer_branch', 'field_id': '1462401000020819915'}
        verify(before, after, allocations, later, fields, cn, inv, schema)
        later['invoices_credited'][0]['credited_amount'] = 0
        with self.assertRaises(BackfillError):
            verify(before, after, allocations, later, fields, cn, inv, schema)
