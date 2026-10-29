"""Archived v3.5 report geometry regressions; v3.6 has a separate contract."""
import unittest,json,base64
from pathlib import Path
from unittest.mock import patch,MagicMock
from decimal import Decimal
import test_regression as fixture
from report_model import build_legacy as build
from cost_basis import calculate,display_average
from security_names import save
from asset_snapshots import capture_page,snapshots,extract_observations,capture_history
from split_entry import register
from ledger import LedgerError
from auto_login import fill_otp
from gmail_auth import message_text
from update_workflow import Workflow

class Revision(unittest.TestCase):
    setUp=fixture.Regression.setUp;tearDown=fixture.Regression.tearDown;trade=fixture.Regression.trade;split=fixture.Regression.split
    def test_reference_geometry_and_dictionary(self):
        self.trade(code='6544');m=build(self.store,self.a)
        t,d=m['sheets'][:2];c={(x['r'],x['c']):x for x in t['cells']}
        self.assertEqual(t['row_height'],20.1);self.assertEqual(t['widths']['1'],13)
        self.assertEqual(c[2,1]['align']['h'],'center');self.assertEqual(c[2,7]['align']['h'],None)
        self.assertIn('配当!$AD$2:$AE$10000',c[2,3]['v']);self.assertFalse(t['filter'])
        dc={(x['r'],x['c']):x['v'] for x in d['cells']}
        self.assertTrue(any('SUM(' in x['v'] and ':' in x['v'] for x in d['cells'] if x['k']=='formula_number' and x['c']==23));self.assertEqual(c[2,3]['fill'],c[2,2]['fill']);self.assertEqual(dc[1,12],'コード');self.assertEqual(dc[1,6],'分類');self.assertIn('JES',dc.values())
        save(self.store.directory,'6544','自分の表記');m=build(self.store,self.a)
        self.assertTrue(any(x['v']=='自分の表記' for x in m['sheets'][1]['cells']))
    def test_split_display_cost_and_buying_budget(self):
        self.trade(price='9660',cash='966000');register(self.store,self.a,'A','1234','2026-02-01',1,3,'試験通知')
        self.trade(day='2026-03-01',side='売',qty='100',price='4000',cash='400000')
        m=build(self.store,self.a);t=m['sheets'][0];c={(x['r'],x['c']):x for x in t['cells']}
        self.assertEqual(c[3,6]['v'],'B');self.assertEqual(c[3,7]['v'],200);self.assertEqual(c[3,9]['v'],0);self.assertIsNone(c[3,6]['fill'])
        state=next(iter(calculate(self.store,self.a)[1].values()))
        self.assertEqual(state['quantity'],200);self.assertEqual(state['cost'],644000);self.assertEqual(display_average(state),3220)
        helper=next(s for s in m['sheets'] if s['name']=='計算明細');self.assertTrue(helper['hidden'])
        self.assertEqual(sum(x['v'] for x in helper['cells'] if x['c']==47 and x['r']>1),966000)
        self.assertFalse(any(x['c']>=35 for x in t['cells']))
    def test_split_duplicate_rejected(self):
        self.trade();register(self.store,self.a,'A','1234','2026-02-01',1,3,'通知')
        with self.assertRaises(LedgerError):register(self.store,self.a,'A','1234','2026-02-01',1,3,'通知')
    def test_assets_table_value_not_profit(self):
        table=[['商品','評価損益','評価額(円)'],['国内株式(現物)','500','1000'],['米国株式','20','200'],['預り金(円)','0','300'],['預り金(米ドル)','0','400'],['ハイブリッド預金','0','500'],['投資信託','0','0'],['円貨建債券','0','0'],['外貨建MMF(米ドル建て)','0','0'],['外貨建債券(米ドル建て)','0','0']]
        capture_page(self.store,'A','更新日時 2026/09/30 16:00',[table])
        s=snapshots(self.store)['A','2026-09-30'];self.assertEqual(s['domestic'],1000);self.assertEqual(s['cash'],800);self.assertEqual(s['foreign_cash'],400)
        self.assertNotIn(('A','2026-10-01'),snapshots(self.store))
    def test_dollars_not_yen_and_unknown_bank(self):
        rows=[['商品','評価額'],['米国株式','100 USD'],['預り金(円)','20円']]
        v=extract_observations('',[rows]);self.assertIsNone(v.get('foreign'))
        capture_page(self.store,'A','評価基準日 2026/10/01\n国内株式\n100円\n投資信託\n0円\n円貨建債券\n0円\n預り金(円)\n20円')
        self.assertIsNone(snapshots(self.store)['A','2026-10-01']['cash'])
    def test_explicit_dated_history(self):
        rows=[['日付','国内株式等(円)','現金合計(円)'],['2026/09/30','100','20'],['2026/10/01','110','25']]
        self.assertEqual(capture_history(self.store,'A',[rows]),2)
        self.assertEqual(snapshots(self.store)['A','2026-10-01']['domestic'],110)
    def test_monthly_change_does_not_infer_calendar_boundary(self):
        from asset_snapshots import capture_month_start,save
        rows=[['商品','評価額(円)','前月比(円)'],['国内株式','1100','+100'],['米国株式','200','-20'],['預り金(円)','300','0'],['ハイブリッド預金','400','+50'],['預り金(米ドル)','0','0']]
        save(self.store,'A','2026-10-01',{'domestic':999},note='手動確認値を保持')
        before=snapshots(self.store)
        self.assertEqual(capture_month_start(self.store,'A','更新日時 2026/10/20',[rows]),0)
        self.assertEqual(snapshots(self.store),before)
        self.assertEqual(snapshots(self.store)['A','2026-10-01']['domestic'],999)
        self.assertNotIn(('A','2026-09-30'),snapshots(self.store))
    def test_six_digit_fields(self):
        fields=[MagicMock() for _ in range(6)];fill_otp(fields,'123456')
        for i,f in enumerate(fields):f.fill.assert_called_once_with(str(i+1))
    def test_shift_jis_mail(self):
        text='認証コード：123456';data=base64.urlsafe_b64encode(text.encode('iso2022_jp')).decode()
        self.assertEqual(message_text({'mimeType':'text/plain','headers':[{'name':'Content-Type','value':'text/plain; charset=iso-2022-jp'}],'body':{'data':data}}),text)
    def test_single_account_collection(self):
        called=[]
        result=Workflow(Path(self.tmp.name)).run(collect=lambda a,s:(called.append(a) or {'complete':True}),review=lambda *a:True,accounts=['B'])
        self.assertEqual(called,['B']);self.assertTrue(result['output'].exists())
    def test_cash_classes(self):
        self.a.add_cash('A','税還付','2026-01-01','100');self.a.add_cash('A','利息','2026-01-02','10')
        d=build(self.store,self.a)['sheets'][1];k=[x['v'] for x in d['cells'] if x['c']==6 and x['r']>1]
        self.assertEqual(k,['利息','税還'])
if __name__=='__main__':unittest.main()
