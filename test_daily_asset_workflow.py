"""Calendar-boundary observations reach existing Excel cells only after review."""
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from acquisition_journal import persist_capture
from activity import Activities
from asset_snapshots import save,snapshots,exact_fields,init as init_assets
from ledger import Store
from pdf_batch import KNOWN_DOCUMENT_EXCLUSIONS,KnownDocumentExclusion
from sbi_views import init_observations
from update_dialog import warning_lines
from update_operations import apply_saved
from update_workflow import Cancelled,fingerprint

JST=timezone(timedelta(hours=9))

class FixedClock(datetime):
    value=None
    @classmethod
    def now(cls,tz=None):return cls.fromtimestamp(cls.value.timestamp(),tz)

def capture_batch(base,day,hour,value):
    with tempfile.TemporaryDirectory(dir=base) as folder:
        store=Store(folder);store.set_owner('A','Anonymous A');init_observations(store)
        start=datetime.fromisoformat(day+'T'+hour+':00+09:00')
        kinds=[('口座サマリー',{'cash_total':300+value,'broker_cash':100+value,'bank':200,
                'growth_remaining':{'year':2026,'remaining':500+value},'tsumitate_remaining':{'year':2026,'remaining':600+value}}),
               ('My資産',{'domestic':value,'fund':0,'bond':0,'foreign':200,'usd_fund':0,'usd_bond':0,'cash':100+value,'bank':200,'foreign_cash':0,'usd_cash':0})]
        with store.db:
            for index,(kind,values) in enumerate(kinds,1):
                stamp=start+timedelta(seconds=index)
                payload={'values':values,'source':kind,'observed_at':stamp.isoformat(),'displayed_at':stamp.strftime('%Y/%m/%d %H:%M:%S')}
                store.db.execute('INSERT INTO account_observations VALUES (?,?,?,?)',('A',kind,stamp.isoformat(),json.dumps(payload)))
        FixedClock.value=start+timedelta(seconds=3)
        with patch('acquisition_journal.datetime',FixedClock):
            persist_capture(base,'A','Anonymous A',store,start.isoformat(),target_dates=('2026-09-30','2026-10-01'))
        store.close()

def excel_cells(path):
    with ZipFile(path) as z:root=ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
    ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    return {cell.attrib['r']:cell for cell in root.findall('.//s:c',ns)},ns

class DailyAssetWorkflow(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name)
        store=Store(self.base/'data');Activities(store);init_observations(store);init_assets(store);store.set_owner('A','Anonymous A');store.close()
        for day,hour,value in [('2026-09-30','09:00',10),('2026-09-30','23:00',11),('2026-10-01','09:00',20),('2026-10-01','17:00',21)]:
            capture_batch(self.base,day,hour,value)

    def test_last_monthend_and_first_day1_observations_use_existing_cells_with_explicit_basis(self):
        seen=[]
        def review(summary,proposal,prepared):
            seen.append(summary)
            self.assertEqual(summary['daily_asset_records'],2)
            warnings=warning_lines(summary['warnings'])
            self.assertTrue(any('当日実測' in line and '終値' in line for line in warnings))
            self.assertTrue(any('ASSET_DATE_INCOMPLETE:A:' in code for code in summary['warnings']))
            cells,ns=excel_cells(prepared)
            self.assertEqual(cells['M2'].findtext('s:v',namespaces=ns),'11.0')
            self.assertEqual(cells['M12'].findtext('s:v',namespaces=ns),'20.0')
            self.assertEqual(cells['R2'].findtext('s:v',namespaces=ns),'511.0')
            self.assertEqual(cells['R12'].findtext('s:v',namespaces=ns),'520.0')
            note=''.join(cells['T2'].itertext())
            self.assertIn('当日実測',note);self.assertIn('23:00',note);self.assertIn('ではない',note)
            return True
        result=apply_saved(self.base,'2026-10',review)
        self.assertEqual(len(seen),1);self.assertTrue(result['output'].is_file())
        store=Store(self.base/'data');rows=snapshots(store)
        self.assertEqual(rows['A','2026-09-30']['domestic'],11)
        self.assertFalse(exact_fields(rows['A','2026-09-30']))
        self.assertEqual(len(store.records()),0);store.close()
        again=apply_saved(self.base,'2026-10',lambda *args:True)
        self.assertEqual(again['summary']['daily_asset_records'],0)

    def test_cancelled_review_preserves_live_database_and_all_capture_journals(self):
        store=Store(self.base/'data');before=fingerprint(store.db);store.close()
        with self.assertRaises(Cancelled):apply_saved(self.base,'2026-10',lambda *args:False)
        store=Store(self.base/'data');self.assertEqual(before,fingerprint(store.db));store.close()
        self.assertEqual(len(list((self.base/'data/acquisitions/A').glob('*.json'))),4)

    def test_manual_partial_snapshot_including_nisa_blank_is_preserved(self):
        store=Store(self.base/'data');save(store,'A','2026-09-30',{'domestic':99},note='Manual basis')
        old=store.db.execute("SELECT payload FROM asset_snapshots WHERE account='A' AND as_of='2026-09-30'").fetchone()[0];store.close()
        apply_saved(self.base,'2026-10',lambda *args:True)
        store=Store(self.base/'data');new=store.db.execute("SELECT payload FROM asset_snapshots WHERE account='A' AND as_of='2026-09-30'").fetchone()[0]
        self.assertEqual(old,new);self.assertIsNone(json.loads(new)['growth_remaining']);store.close()

    def test_known_nisa_notice_warns_before_review_and_does_not_create_sale_or_transfer(self):
        folder=self.base/'data/電子ポストPDF/A';folder.mkdir(parents=True);(folder/'notice.pdf').write_bytes(b'known-nisa-fixture')
        rule=next(rule for rule in KNOWN_DOCUMENT_EXCLUSIONS if rule.code=='NISA_TRANSFER_OUT')
        seen=[]
        def review(summary,proposal,prepared):
            seen.extend(warning_lines(summary['warnings']))
            self.assertTrue(any('口座区分移動・取得価額は未反映' in line for line in seen))
            self.assertTrue(any('NISAからの払出し' in row['state'] for row in proposal))
            return True
        with patch('update_workflow.classify',side_effect=KnownDocumentExclusion(rule)):
            result=apply_saved(self.base,'2026-10',review)
        self.assertEqual(result['summary']['new_trades'],0);self.assertTrue((folder/'notice.pdf').is_file())

    def test_legacy_monthly_estimate_is_preserved_but_never_described_as_confirmed(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);store=Store(base/'data');Activities(store)
            save(store,'A','2026-09-30',{'domestic':999},source='SBI My資産の前月比',note='Legacy monthly inference')
            payload=store.db.execute("SELECT payload FROM asset_snapshots WHERE account='A'").fetchone()[0];store.close()
            def review(summary,proposal,prepared):
                self.assertTrue(any('ASSET_DATE_LEGACY_ESTIMATE:A:' in code for code in summary['warnings']))
                cells,_=excel_cells(prepared)
                self.assertIn('指定日残高は未確認',''.join(cells['T2'].itertext()))
                return True
            apply_saved(base,'2026-10',review)
            store=Store(base/'data')
            self.assertEqual(payload,store.db.execute("SELECT payload FROM asset_snapshots WHERE account='A'").fetchone()[0]);store.close()

if __name__=='__main__':unittest.main()
