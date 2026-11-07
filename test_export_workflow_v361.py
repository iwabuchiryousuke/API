"""Excel-button reference refresh: no SBI login and no financial rewrites."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from export_workflow import export_with_research
from test_data_compatibility import seed_fixture
from update_workflow import Cancelled


class PublicReferenceExport(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        self.store,self.activities=seed_fixture(self.base);self.addCleanup(self.store.close)

    def financial(self):
        return {name:self.store.db.execute('SELECT * FROM '+name+' ORDER BY rowid').fetchall()
                for name in ('accounts','imports','trades','activities','evidence')}

    def test_export_refreshes_dividend_only_codes_and_keeps_ledger_unchanged(self):
        self.activities.add_cash('B','配当','2026-10-02','20','Synthetic',code='9901',quantity='10',per_share='2')
        before=self.financial();refresh=Mock(return_value={'failed':[]});progress=[]
        result=export_with_research(self.base,'2026-10',refresh=refresh,progress=progress.append)
        self.assertTrue(result['output'].is_file())
        self.assertFalse(result['public_research_incomplete'])
        directory,codes,month=refresh.call_args.args
        self.assertEqual(directory,self.store.directory);self.assertEqual(month,'2026-10')
        self.assertIn('9901',codes);self.assertIn('9900',codes)
        self.assertEqual(before,self.financial());self.assertEqual(len(progress),2)

    def test_public_fetch_failure_still_exports_actual_receipts_without_fake_cash(self):
        before=self.financial()
        result=export_with_research(self.base,'2026-10',refresh=Mock(side_effect=OSError('Synthetic public outage')))
        self.assertTrue(result['output'].is_file());self.assertTrue(result['public_research_incomplete'])
        self.assertEqual(before,self.financial())
        from zipfile import ZipFile
        with ZipFile(result['output']) as package:
            self.assertNotIn(b'<pane',package.read('xl/worksheets/sheet1.xml'))

    def test_bad_period_does_not_contact_public_site_or_create_output(self):
        refresh=Mock()
        with self.assertRaises(ValueError):export_with_research(self.base,'bad-period',refresh=refresh)
        refresh.assert_not_called();self.assertFalse((self.base/'output').exists())

    def test_cancellation_before_and_after_refresh_does_not_create_output(self):
        refresh=Mock()
        with self.assertRaises(Cancelled):export_with_research(self.base,'2026-10',refresh=refresh,cancelled=lambda:True)
        refresh.assert_not_called()
        cancelled=[False]
        def update(*args,**kwargs):cancelled[0]=True;return {'failed':[]}
        with self.assertRaises(Cancelled):
            export_with_research(self.base,'2026-10',refresh=update,cancelled=lambda:cancelled[0])
        self.assertFalse((self.base/'output').exists())

    def test_offline_saved_pdf_review_uses_saved_order_and_can_refresh_public_references(self):
        from dividend_order import save
        from update_operations import apply_saved
        save(self.store.directory,['9901','9900'])
        seen=[]
        def refresh(stage,month):
            from dividend_order import load
            seen.append((load(stage.directory),month));return {'failed':[]}
        result=apply_saved(self.base,'2026-10',lambda *args:True,current_only=True,reference_refresh=refresh)
        self.assertEqual(seen,[(['9901','9900'],'2026-10')])
        self.assertTrue(Path(result['output']).is_file())


if __name__=='__main__':unittest.main()
