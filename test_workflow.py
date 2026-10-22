import tempfile,unittest
from pathlib import Path
from ledger import Store
from activity import Activities
from update_workflow import Workflow,UpdateError,Cancelled,exclusive
class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name)
        s=Store(self.base/'data');Activities(s);s.close()
    def tearDown(self):self.tmp.cleanup()
    def test_empty_export_and_repeat(self):
        for _ in range(2):
            r=Workflow(self.base).run(review=lambda *a:True)
            self.assertTrue(r['output'].exists());self.assertEqual(r['summary']['new_trades'],0)
    def test_review_required(self):
        with self.assertRaises(Cancelled):Workflow(self.base).run()
        self.assertFalse((self.base/'output').exists())
    def test_failed_collection(self):
        with self.assertRaises(UpdateError) as e:Workflow(self.base).run(collect=lambda *a:{'complete':False},review=lambda *a:True)
        self.assertEqual(e.exception.code,'COLLECTION_INCOMPLETE')
    def test_concurrent_change(self):
        def review(*a):
            s=Store(self.base/'data');s.rename('A','Changed');s.close();return True
        with self.assertRaises(UpdateError) as e:Workflow(self.base).run(review=review)
        self.assertEqual(e.exception.code,'LEDGER_CHANGED')
    def test_lock(self):
        with exclusive(self.base/'data'/'update.lock'):
            with self.assertRaises(UpdateError) as e:Workflow(self.base).run()
            self.assertEqual(e.exception.code,'BUSY')
    def test_mid_commit_rollback(self):
        s=Store(self.base/'data');Activities(s)
        from asset_snapshots import init
        from update_workflow import fingerprint
        init(s);expected=fingerprint(s.db)
        t=Store(self.base/'stage');s.db.backup(t.db);t.rename('A','Staged')
        t.db.execute('DROP TABLE trades');t.db.commit()
        with self.assertRaises(Exception):Workflow(self.base)._commit(s,t,expected)
        self.assertEqual(fingerprint(s.db),expected);s.close();t.close()
    def test_cancel_before_collection(self):
        with self.assertRaises(Cancelled):Workflow(self.base,cancelled=lambda:True).run(collect=lambda *a:self.fail())
    def test_source_invalid(self):
        folder=self.base/'data'/'電子ポストPDF'/'E';folder.mkdir(parents=True);(folder/'bad.pdf').write_bytes(b'bad')
        with self.assertRaises(Exception):Workflow(self.base).run(review=lambda *a:True)
        self.assertFalse((self.base/'output').exists())
if __name__=='__main__':unittest.main()
