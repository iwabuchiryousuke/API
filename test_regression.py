"""Anonymous financial and persistence regressions; no private PDF fixtures."""
import unittest,tempfile,hashlib,json,base64
from pathlib import Path
from decimal import Decimal as D
from unittest.mock import patch
from ledger import Store,Snapshot,LedgerError
from activity import Activities
from cost_basis import calculate,display_average
from custody_labels import label
from report_model import build
from standalone_excel import export_trades
from asset_snapshots import save,snapshots,parse_asset_text
from gmail_auth import extract_code
from smbc_import import era_date

class Regression(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'data');self.a=Activities(self.store)
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def trade(self,day='2026-01-01',side='買',qty='100',price='100',cash='10000',account='A',custody='NISA',code='1234',name='試験株',fees='0'):
        t=dict(trade_date=day,settlement_date=day,name=name,code=code,market='東証',side=side,custody=custody,tax_class='',quantity=qty,price=price,fees=fees,tax='0',settlement_amount=cash)
        all_trades=[{k:v for k,v in r.items() if k not in ('record_id','account','account_label','source')} for r in self.store.records() if r['account']==account and r['trade_date']==day]+[t]
        raw=json.dumps(all_trades).encode();s=Snapshot(raw,'anonymous.csv',hashlib.sha256(raw).hexdigest(),day,day,all_trades)
        self.store.import_snapshot(account,s);return s
    def split(self,day='2026-02-01',before=100,added=200,after=300):
        e=dict(type='株式分割',date=day,account='A',account_label='俊吾',name='試験株',code='1234',before=before,added=added,after=after,ratio_old=1,ratio_new=3)
        self.store.db.execute('INSERT INTO activities(account,kind,event_key,event_date,payload,source) VALUES (?,?,?,?,?,?)',('A','株式分割','split',day,json.dumps(e),'test'));self.store.db.commit()
    def state(self):return calculate(self.store,self.a)[1]
    def test_split_then_sale(self):
        self.trade(price='9660',cash='966000');self.split();self.trade(day='2026-03-01',side='売',qty='100',price='4000',cash='400000')
        results,state=calculate(self.store,self.a);s=next(iter(state.values()));self.assertEqual(s['quantity'],200);self.assertEqual(s['cost'],644000);self.assertEqual(display_average(s),3220);self.assertIn(D(78000),[r.get('reference_gain') for r in results.values()])
    def test_partial_sale_average(self):
        self.trade();self.trade(day='2026-02-01',side='売',qty='30',cash='6000',price='200');s=next(iter(self.state().values()));self.assertEqual(s['cost'],7000);self.assertEqual(display_average(s),100)
    def test_two_purchase_weighted(self):
        self.trade(qty='60',price='20700',cash='1243650',fees='1650');self.trade(day='2026-01-02',qty='15',price='20600',cash='309440',fees='440');s=next(iter(self.state().values()));self.assertEqual(s['cost'],1553090);self.assertEqual(s['quantity'],75)
    def test_missing_purchase(self):
        self.trade(side='売');s=next(iter(self.state().values()));self.assertFalse(s['valid']);self.assertIsNone(display_average(s))
    def test_same_day_mixed(self):
        self.trade();self.trade(side='売',qty='20',cash='2000');self.assertFalse(next(iter(self.state().values()))['valid'])
    def test_account_separation(self):
        self.trade();self.trade(account='B',price='200',cash='20000');self.assertEqual(len(self.state()),2)
    def test_custody_separation(self):
        self.trade();self.trade(custody='特定',price='200',cash='20000');self.assertEqual(len(self.state()),2)
    def test_code_less_funds_separate(self):
        self.trade(code='',name='基金一');self.trade(code='',name='基金二');self.assertEqual(len(self.state()),2)
    def test_split_mismatch(self):
        self.trade();self.split(before=200,added=400,after=600);self.assertFalse(next(iter(self.state().values()))['valid'])
    def test_split_no_history(self):
        self.split();self.assertFalse(next(iter(self.state().values()))['valid'])
    def test_old_nisa(self):self.assertEqual(label('NISA','2023-12-31','買'),'旧NISA')
    def test_growth_nisa(self):self.assertEqual(label('NISA','2026-01-01','買'),'NISA(成)')
    def test_generic_sale_nisa(self):self.assertEqual(label('NISA','2026-01-01','売'),'NISA(要確認)')
    def test_nisa_fund_unknown(self):self.assertEqual(label('NISA','2026-01-01','買','投資信託'),'NISA(要確認)')
    def test_csv_repeat(self):
        s=self.trade();self.store.import_snapshot('A',s);self.assertEqual(len(self.store.records()),1)
    def test_cash_repeat(self):
        self.assertTrue(self.a.add_cash('E','配当','2026-01-01','100','試験株',code='1234'));self.assertFalse(self.a.add_cash('E','配当','2026-01-01','100','試験株',code='1234'))
    def test_cash_invalid(self):
        for v in ('-1','NaN','Infinity','abc'):
            with self.subTest(v=v),self.assertRaises(LedgerError):self.a.add_cash('E','利息','2026-01-01',v)
    def test_dividend_gross(self):
        self.a.add_cash('E','配当','2026-01-01','80','試験株',code='1234',tax='20',quantity='10',per_share='10');e=self.a.events()[0];self.assertEqual(e['gross'],'100');self.assertEqual(e['net'],'80')
    def test_dividend_mismatch(self):
        with self.assertRaises(LedgerError):self.a.add_cash('E','配当','2026-01-01','80','試験株',quantity='10',per_share='10')
    def test_dividend_only_unknown(self):
        self.a.add_cash('E','配当','2026-01-01','80','試験株',code='1234');self.assertFalse(next(iter(self.state().values()))['valid'])
    def test_snapshot_no_overwrite(self):
        save(self.store,'A','2026-09-30',{'domestic':100});self.assertIsNone(snapshots(self.store)[('A','2026-09-30')]['cash'])
        with self.assertRaises(LedgerError):save(self.store,'A','2026-09-30',{'domestic':200})
    def test_father_asset_manual(self):
        with self.assertRaises(LedgerError):save(self.store,'E','2026-09-30',{})
    def test_buying_power_not_assets(self):
        with self.assertRaises(LedgerError):parse_asset_text('買付余力\n1,000円')
    def test_two_sheets_and_order(self):
        self.trade();self.trade(day='2026-02-01');self.a.add_cash('A','利息','2026-01-05','10')
        m=build(self.store,self.a);self.assertEqual([s['name'] for s in m['sheets'] if not s.get('hidden')],['取引','配当'])
        cells={(c['r'],c['c']):c for c in m['sheets'][0]['cells']};self.assertEqual(cells[(2,1)]['v'],'2026-02-01');self.assertEqual(cells[(1,1)]['v'],'日付');self.assertEqual(cells[(2,6)]['v'],'B')
    def test_output_valid_xml(self):
        from zipfile import ZipFile
        from xml.etree import ElementTree as ET
        self.trade();p=export_trades(self.store,self.tmp.name,management=True)
        with ZipFile(p) as z:
            for n in z.namelist():
                if n.endswith('.xml') or n.endswith('.rels'):ET.fromstring(z.read(n))
            self.assertIn(b'topLeftCell="C2"',z.read('xl/worksheets/sheet1.xml'));self.assertIn(b'Times New Roman',z.read('xl/styles.xml'))
    def test_balance_owner_required(self):
        from types import SimpleNamespace
        doc=SimpleNamespace(kind='balance_pdf',owner='別の名義')
        with self.assertRaises(LedgerError):self.a.import_balance('A',doc)
    def test_owner_mismatch(self):
        s=self.trade();s.kind='pdf';s.digest='other';s.owner='別人';s.document_key='doc'
        with self.assertRaises(LedgerError):self.store.preview('A',s)
    def test_formula_cash_code_less(self):
        self.a.add_cash('E','配当','2026-01-01','100','不明配当');m=build(self.store,self.a);self.assertTrue(any(c['v']=='未分類' for c in m['sheets'][1]['cells']))
    def test_forecast_entitlement_not_current(self):
        from dividend_forecast import forecast
        self.trade();self.trade(day='2026-02-01',side='売',qty='50',cash='5000')
        facts={'1234':{'calendar_complete':True,'events':[{'rights':'2026-01-30','payment':'2026-03-01','dps':10}]}}
        amounts,lines=forecast(self.store,self.a,facts,'2026-02-15');self.assertEqual(amounts['1234'],1000)
    def test_forecast_next_year(self):
        from dividend_forecast import forecast
        self.trade();facts={'1234':{'calendar_complete':True,'events':[{'rights':'2026-12-28','payment_year':2027,'dps':10}]}}
        amounts,_=forecast(self.store,self.a,facts,'2026-10-16');self.assertEqual(amounts['1234'],0)
    def test_forecast_taxable_unknown(self):
        from dividend_forecast import forecast
        self.trade(custody='特定');facts={'1234':{'calendar_complete':True,'events':[{'rights':'2026-01-30','payment':'2026-03-01','dps':10}]}}
        amounts,_=forecast(self.store,self.a,facts,'2026-02-15');self.assertIsNone(amounts['1234'])
    def test_batch_failure_rollback(self):
        from pdf_batch import import_all
        from types import SimpleNamespace
        raw=b'fixture';path=Path(self.tmp.name)/'fixture.pdf';path.write_bytes(raw)
        doc=SimpleNamespace(digest=hashlib.sha256(raw).hexdigest())
        def fake(store,*args):
            self.a.add_cash('A','利息','2026-01-01','10');raise OSError('mock disk failure')
        with patch('pdf_batch._apply',side_effect=fake),self.assertRaises(LedgerError):import_all(self.store,'A',[(path,'notice',doc)])
        self.assertEqual(self.a.events(),[])
    def test_renamed_account_persists(self):
        self.store.rename('D','母(雅美)');self.store.close();self.store=Store(Path(self.tmp.name)/'data');self.assertEqual(self.store.accounts()[3]['label'],'母(雅美)')
    def test_smbc_era(self):self.assertEqual(era_date('8.9.7'),'2026-09-07')
    def test_otp_matching(self):
        msg={'internalDate':'200000','payload':{'mimeType':'text/plain','headers':[{'name':'From','value':'info@sbisec.co.jp'},{'name':'To','value':'test@example.com'},{'name':'Subject','value':'ログイン認証'},{'name':'Authentication-Results','value':'mx.google.com; dkim=pass header.i=@sbisec.co.jp;'}],'body':{'data':base64.urlsafe_b64encode('認証コード：123456'.encode()).decode()}}}
        self.assertEqual(extract_code(msg,'test@example.com',100),'123456');self.assertIsNone(extract_code(msg,'other@example.com',100));self.assertIsNone(extract_code(msg,'test@example.com',201))
        msg['payload']['headers'][-1]['value']='dkim=fail header.i=@sbisec.co.jp';self.assertIsNone(extract_code(msg,'test@example.com',100))
    def test_month_cutoff(self):
        self.trade(day='2026-01-01');self.trade(day='2026-02-01');m=build(self.store,self.a,'2026-01');dates=[c['v'] for c in m['sheets'][0]['cells'] if c['c']==1 and c['k']=='date'];self.assertEqual(dates,['2026-01-01'])
    def test_existing_data_reopen(self):
        self.trade();self.store.set_owner('A','試験名義');self.store.close();self.store=Store(Path(self.tmp.name)/'data');self.assertEqual(len(self.store.records()),1);self.assertEqual(self.store.accounts()[0]['owner_name'],'試験名義')

if __name__=='__main__':unittest.main(verbosity=2)
