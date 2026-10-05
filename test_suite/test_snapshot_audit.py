import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from openpyxl import Workbook
from common.config import OUTPUT_COLUMNS, TEMPLATE_SHEET

spec = importlib.util.spec_from_file_location('snapshot_audit', Path(__file__).resolve().parents[1] / 'scripts/audit_snapshot.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SnapshotAuditTests(unittest.TestCase):
    def fixture(self, directory):
        root = Path(directory)
        location, identity = 'Texas (Fixture)', 'FixtureScraper|event-1'
        output = module.destination(root / 'Snapshots', '2026-10-04', location)
        output.parent.mkdir(parents=True)
        wb = Workbook()
        ws = wb.active
        ws.title = TEMPLATE_SHEET
        ws.append(OUTPUT_COLUMNS)
        data = {'Tournament-name': 'Future Classic', 'Start-date': '07/01/2027', 'End-date': '07/03/2027',
                'State-FullName': 'Texas', 'Website-url': 'https://example.com/event', 'Address': 'N/A'}
        ws.append([data.get(key, '') for key in OUTPUT_COLUMNS])
        index = wb.create_sheet('_ScraperExportIndex')
        index.sheet_state = 'hidden'
        index.append(['Event identity', 'Import row', 'Website URL', 'Tournament name'])
        index.append([identity, 2, data['Website-url'], data['Tournament-name']])
        wb.save(output)
        wb.close()
        manifest = {'run_id': 'fixture', 'run_date': '2026-10-04', 'mode': 'snapshot', 'status': 'complete',
                    'output_root': str(root / 'Snapshots'), 'providers': {'FixtureScraper': {'status': 'complete', 'locations': [location]}},
                    'locations': {'FixtureScraper|' + location: {'status': 'complete', 'collection_complete': True,
                                  'links_collected': 1, 'rows_written': 1}}}
        ledger = {'events': {identity: {'scraper': 'FixtureScraper', 'locations': [location], 'export_file': str(output),
                                      'export_status': 'exported', 'event_data': data}}}
        mpath, lpath = root / 'manifest.json', root / 'ledger.json'
        mpath.write_text(json.dumps(manifest), encoding='utf-8')
        lpath.write_text(json.dumps(ledger), encoding='utf-8')
        return mpath, lpath, manifest, ledger

    def test_healthy_workbook_and_identity_index_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, ledger, _, _ = self.fixture(directory)
            result = module.audit(manifest, ledger)
            self.assertTrue(result['passed'], result['problems'])
            self.assertEqual(result['rows'], 1)

    def test_count_mismatch_and_pending_acknowledgement_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            mpath, lpath, manifest, ledger = self.fixture(directory)
            next(iter(manifest['locations'].values()))['rows_written'] = 2
            next(iter(ledger['events'].values()))['export_status'] = 'pending'
            mpath.write_text(json.dumps(manifest), encoding='utf-8')
            lpath.write_text(json.dumps(ledger), encoding='utf-8')
            result = module.audit(mpath, lpath)
            self.assertFalse(result['passed'])
            self.assertTrue(any('manifest rows' in problem for problem in result['problems']))
            self.assertTrue(any('Unacknowledged export' in problem for problem in result['problems']))

    def test_verified_empty_location_can_omit_workbook(self):
        with tempfile.TemporaryDirectory() as directory:
            mpath, lpath, manifest, _ = self.fixture(directory)
            manifest['providers']['FixtureScraper']['locations'].append('Alaska (Fixture)')
            manifest['locations']['FixtureScraper|Alaska (Fixture)'] = {'status': 'complete', 'collection_complete': True,
                                                                     'links_collected': 0, 'rows_written': 0}
            mpath.write_text(json.dumps(manifest), encoding='utf-8')
            result = module.audit(mpath, lpath)
            self.assertTrue(result['passed'], result['problems'])
            self.assertEqual(result['verified_empty_locations_without_workbooks'], 1)
