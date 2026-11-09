"""Recovery of a legacy first-item-only PDF through the real saved-PDF workflow."""
import json
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from activity import Activities,key
from asset_snapshots import init as init_assets
from corporate_actions import parse_notice
from ledger import LedgerError,Store
from pdf_batch import import_all,preflight
from sbi_views import init_observations
from test_multi_dividend_pdf_v363 import notice_lines,write_notice_pdf
from update_operations import apply_saved
from update_workflow import Cancelled,fingerprint


class SavedNoticeRecovery(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.store=Store(self.base/'data')
        self.addCleanup(self.store.close);self.activities=Activities(self.store)
        self.store.set_owner('A','架空 試験')
        init_assets(self.store);init_observations(self.store)
        self.path=self.base/'data/電子ポストPDF/A/anonymous.pdf'
        self.path.parent.mkdir(parents=True)
        write_notice_pdf(self.path,notice_lines());self.document=parse_notice(self.path)

    def seed_legacy_first_only(self):
        event=self.document.item
        with self.store.db:
            ident=self.store.db.execute('INSERT INTO activities(account,kind,event_key,event_date,payload,source) VALUES (?,?,?,?,?,?)',
                ('A',event['type'],key(event),event['date'],json.dumps(event,ensure_ascii=False),self.path.name)).lastrowid
            anchor=self.store.db.execute('INSERT INTO activity_files(account,digest,activity_id) VALUES (?,?,?)',
                ('A',self.document.digest,ident)).lastrowid
        archive=self.store.directory/'originals/A'/f'{self.document.digest}.pdf'
        archive.parent.mkdir(parents=True);archive.write_bytes(self.document.raw)
        return ident,anchor,self.store.db.execute('SELECT payload FROM activities WHERE id=?',(ident,)).fetchone()[0]

    def assert_dividend_detail(self,path):
        ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        with ZipFile(path) as workbook:
            sheet=ET.fromstring(workbook.read('xl/worksheets/sheet2.xml'))
            self.assertFalse(any(name.startswith('xl/externalLinks/') for name in workbook.namelist()))
        rows=[]
        for row in sheet.findall('.//s:sheetData/s:row',ns):
            cells={cell.attrib['r'].rstrip('0123456789'):cell for cell in row.findall('s:c',ns)}
            if 'D' in cells and 'I' in cells:
                code=cells['D'].findtext('s:v',namespaces=ns)
                value=cells['I'].findtext('s:v',namespaces=ns)
                if code in ('9900','9911'):rows.append((code,Decimal(value)))
        self.assertEqual(sorted(rows),[('9900',Decimal('35')),('9911',Decimal('30'))])

    def test_known_old_digest_recovers_missing_item_and_preserves_original_id_and_payload(self):
        ident,anchor,payload=self.seed_legacy_first_only()
        self.assertFalse(self.activities.notice_is_complete('A',self.document.digest))
        reviews=[]
        def review(summary,proposal,prepared):
            reviews.append(summary);self.assert_dividend_detail(prepared)
            self.assertEqual(summary['new_cash_or_splits'],1)
            notice=next(item for item in proposal if item['kind']=='notice')
            self.assertEqual((notice['new'],notice['existing']),(1,1))
            self.assertIn('2明細',notice['state']);return True
        result=apply_saved(self.base,'',review,current_only=True)
        self.assertEqual(len(reviews),1);self.assert_dividend_detail(result['output'])
        self.assertEqual(len(self.activities.events()),2)
        self.assertEqual(self.store.db.execute('SELECT payload FROM activities WHERE id=?',(ident,)).fetchone()[0],payload)
        self.assertEqual(self.store.db.execute('SELECT id,activity_id FROM activity_files WHERE account=? AND digest=?',
                                              ('A',self.document.digest)).fetchone()[:],(anchor,ident))
        self.assertTrue(self.activities.notice_is_complete('A',self.document.digest))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM activity_file_events').fetchone()[0],2)
        self.assertEqual(self.path.read_bytes(),self.document.raw)
        with patch('update_workflow.classify',side_effect=AssertionError('Complete notice should reuse its verified manifest')):
            again=apply_saved(self.base,'',lambda *args:True,current_only=True)
        self.assertEqual(again['summary']['new_cash_or_splits'],0)
        self.assertEqual(again['summary']['known_pdf_files'],1)
        self.assertEqual(len(self.activities.events()),2)

    def test_cancelled_recovery_does_not_create_schema_or_modify_legacy_database(self):
        self.seed_legacy_first_only()
        before=fingerprint(self.store.db);before_bytes=(self.store.directory/'ledger.sqlite3').read_bytes()
        with self.assertRaises(Cancelled):apply_saved(self.base,'',lambda *args:False,current_only=True)
        self.assertEqual(fingerprint(self.store.db),before)
        self.assertEqual((self.store.directory/'ledger.sqlite3').read_bytes(),before_bytes)
        self.assertEqual(len(self.activities.events()),1)
        self.assertFalse(self.store.db.execute("SELECT 1 FROM sqlite_master WHERE name='activity_documents'").fetchone())

    def test_conflicting_existing_first_item_cannot_partially_add_second(self):
        ident,_,_=self.seed_legacy_first_only()
        other=dict(self.document.item,net='34')
        with self.store.db:
            self.store.db.execute('UPDATE activities SET payload=? WHERE id=?',(json.dumps(other,ensure_ascii=False),ident))
        before=fingerprint(self.store.db)
        with self.assertRaises(LedgerError):apply_saved(self.base,'',lambda *args:True,current_only=True)
        self.assertEqual(fingerprint(self.store.db),before)
        self.assertEqual(len(self.activities.events()),1)

    def test_fresh_file_imports_all_rows_in_one_confirmation(self):
        result=apply_saved(self.base,'',lambda summary,*args:summary['new_cash_or_splits']==2,current_only=True)
        self.assertEqual(result['summary']['new_cash_or_splits'],2)
        self.assertEqual(len(self.activities.events()),2)
        self.assert_dividend_detail(result['output'])

    def test_manual_pdf_preflight_counts_every_item_without_modifying_live_data(self):
        before=fingerprint(self.store.db)
        pending=preflight(self.store,'A',[self.path])
        self.assertEqual((pending[0][3],pending[0][4],pending[0][5]),(2,0,False))
        self.assertEqual(fingerprint(self.store.db),before)
        self.assertFalse(self.store.db.execute("SELECT 1 FROM sqlite_master WHERE name='activity_documents'").fetchone())
        self.assertEqual(import_all(self.store,'A',pending),[(2,0,False)])
        self.assertEqual(import_all(self.store,'A',pending),[(0,2,True)])
        self.assertEqual(len(self.activities.events()),2)


if __name__=='__main__':unittest.main()
