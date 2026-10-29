"""Minimal manual cash entries can acquire later PDF evidence without doubling."""
import hashlib,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace

from activity import Activities
from app import add_manual_cash
from ledger import LedgerError,Store


class ManualCashReconciliation(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'data');self.addCleanup(self.store.close)
        self.store.set_owner('A','Example A');self.activities=Activities(self.store)

    def document(self,kind='税還付',amount='100'):
        event=dict(type=kind,date='2026-10-29',name='PDF cash notice',code='',amount=amount,
                   currency='JPY',description='Parsed notice')
        raw=json.dumps(event).encode()
        return SimpleNamespace(owner='Example A',item=event,raw=raw,
                               digest=hashlib.sha256(raw).hexdigest(),filename='synthetic.pdf')

    def test_pdf_evidence_attaches_to_exact_manual_credit_without_replacing_payload(self):
        for display,kind in (('税還','税還付'),('利息','利息')):
            with self.subTest(kind=kind):
                self.assertTrue(add_manual_cash(self.activities,'A','2026-10-29',display,'100'))
                doc=self.document(kind)
                before=[row for row in self.activities.events() if row['type']==kind][0]
                result=self.activities.import_notice('A',doc)
                self.assertTrue(result['same_event'])
                rows=[row for row in self.activities.events() if row['type']==kind]
                self.assertEqual(len(rows),1)
                after=dict(rows[0]);after['source']=before['source']
                self.assertEqual(after,before)
                self.assertTrue(self.activities.import_notice('A',doc)['already'])
                self.assertEqual(len([row for row in self.activities.events() if row['type']==kind]),1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM activity_files').fetchone()[0],2)

    def test_different_amount_still_requires_review_instead_of_overwriting_manual_credit(self):
        add_manual_cash(self.activities,'A','2026-10-29','税還','100')
        before=self.activities.events()
        with self.assertRaises(LedgerError):self.activities.import_notice('A',self.document(amount='101'))
        self.assertEqual(self.activities.events(),before)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM activity_files').fetchone()[0],0)
