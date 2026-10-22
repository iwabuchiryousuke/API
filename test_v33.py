import base64,json,unittest
from unittest.mock import patch
import test_regression as fixture
from report_model import build,reporting_code
from gmail_auth import extract_code,extract_auth_link
from auto_login import device_screen_code,trusted
from login_credentials import CredentialError
from types import SimpleNamespace

class Changes(unittest.TestCase):
    setUp=fixture.Regression.setUp
    tearDown=fixture.Regression.tearDown
    trade=fixture.Regression.trade
    split=fixture.Regression.split
    def test_split_export_uses_same_custody_as_cost(self):
        self.trade();self.split()
        m=build(self.store,self.a,'2026-03',True)
        c={(x['r'],x['c']):x['v'] for x in m['sheets'][0]['cells']}
        self.assertEqual(c[2,6],'B');self.assertEqual(c[2,5],'N-成');self.assertEqual(c[2,7],200)
    def test_code_less_report_keys_distinct(self):
        self.trade(code='',name='基金一');self.trade(code='',name='基金二')
        m=build(self.store,self.a)
        keys=[c['v'] for c in m['sheets'][0]['cells'] if c['c']==4 and c['r']>1]
        self.assertEqual(len(set(keys)),2);self.assertTrue(all(k.startswith('F-') for k in keys))
    def test_no_duplicate_excel_coordinates(self):
        self.trade();m=build(self.store,self.a,management=True)
        for s in m['sheets']:
            pairs=[(c['r'],c['c']) for c in s['cells']];self.assertEqual(len(pairs),len(set(pairs)))
    def test_management_reference_stable_after_reordering(self):
        self.trade();m=build(self.store,self.a,management=True)
        cells=m['sheets'][2]['cells'];start=next(c['r'] for c in cells if c['c']==10 and c['v']=='取得総額')
        formulas=[c['v'] for c in cells if c['r']==start+1 and c['k'].startswith('formula')]
        self.assertTrue(any('COUNTIFS' in f for f in formulas));self.assertFalse(any('取引!AA2' in f for f in formulas))
    def test_nisa_uses_settlement_date(self):
        self.trade(day='2026-12-30');m=build(self.store,self.a,'2026-12',True)
        formulas=[c['v'] for c in m['sheets'][2]['cells'] if c['k'].startswith('formula')]
        self.assertTrue(any('計算明細!$AT$2:$AT$10000' in f for f in formulas))
        dates=[c for c in next(s for s in m['sheets'] if s['name']=='計算明細')['cells'] if c['c']==46 and c['r']==2]
        self.assertEqual(dates[0]['k'],'date')
    def test_no_phantom_security_from_research(self):
        self.trade(code='1234')
        with patch('market_data.load_research',return_value={'securities':{'9999':{'name':'not owned'}}}):m=build(self.store,self.a)
        self.assertFalse(any(c['v']=='not owned' for s in m['sheets'] for c in s['cells']))
    def test_father_holdings_manual(self):
        self.trade(account='E');m=build(self.store,self.a,management=True)
        cells=m['sheets'][2]['cells'];r=next(c['r'] for c in cells if c['v']=='父：手入力')
        self.assertTrue(all(c['v'] is None for c in cells if c['r']==r and c['c'] in (5,6,7,9,10)))
    def test_device_code_from_screen(self):
        self.assertEqual(device_screen_code('認証コード\n123456\n残り30秒'),'123456')
        with self.assertRaises(CredentialError):device_screen_code('認証コード 123456\n認証コード 654321')
    def test_https_required(self):
        self.assertFalse(trusted(SimpleNamespace(url='http://m.sbisec.co.jp/')))
        self.assertFalse(trusted(SimpleNamespace(url='https://sbisec.co.jp.evil.example/')))
        self.assertTrue(trusted(SimpleNamespace(url='https://m.sbisec.co.jp/')))
    def test_gmail_link_html(self):
        def msg(url):return {'internalDate':'200000','payload':{'mimeType':'text/html','headers':[{'name':'From','value':'info@sbisec.co.jp'},{'name':'To','value':'test@example.com'},{'name':'Subject','value':'認証コード入力画面のお知らせ（メインサイトログイン時のデバイス認証）'},{'name':'Authentication-Results','value':'mx.google.com; dkim=pass header.i=@sbisec.co.jp;'}],'body':{'data':base64.urlsafe_b64encode(f'<a href="{url}">確認</a>'.encode()).decode()}}}
        url='https://m.sbisec.co.jp/device?sample=fixture'
        self.assertEqual(extract_auth_link(msg(url),'test@example.com',100),url)
        self.assertIsNone(extract_auth_link(msg('https://m.sbisec.co.jp.evil.example/login'),'test@example.com',100))
        self.assertIsNone(extract_auth_link(msg(url),'other@example.com',100))
        self.assertIsNone(extract_auth_link(msg(url),'test@example.com',201))
    def test_receiver_auth_required(self):
        m={'internalDate':'200000','payload':{'mimeType':'text/plain','headers':[{'name':'From','value':'info@sbisec.co.jp'},{'name':'To','value':'test@example.com'},{'name':'Subject','value':'ログイン認証'},{'name':'Authentication-Results','value':'evil.example; dkim=pass header.i=@sbisec.co.jp;'}],'body':{'data':base64.urlsafe_b64encode('認証コード：123456'.encode()).decode()}}}
        self.assertIsNone(extract_code(m,'test@example.com',100))
    def test_asset_capture_retry_is_idempotent(self):
        from asset_snapshots import save,snapshots
        from ledger import LedgerError
        amounts=dict(domestic=10,foreign=0,cash=20,foreign_cash=0)
        save(self.store,'A','2026-09-30',amounts)
        self.assertFalse(save(self.store,'A','2026-09-30',amounts))
        with self.assertRaises(LedgerError):save(self.store,'A','2026-09-30',dict(amounts,cash=30))
        self.assertEqual(snapshots(self.store)['A','2026-09-30']['cash'],20)
    def test_forecast_helpers_do_not_overlap_many_securities(self):
        for i in range(45):self.trade(code=str(1000+i))
        m=build(self.store,self.a,management=True)
        cells=m['sheets'][1]['cells']
        header=next(c['r'] for c in cells if c['c']==36 and c['v']=='権利最終日')
        self.assertGreater(header,46)
    def test_device_auth_workflow_screen_code_not_mail_code(self):
        from auto_login import device_auth
        from unittest.mock import MagicMock
        page=MagicMock();page.locator.return_value.inner_text.return_value='認証コード 123456'
        page.get_by_role.return_value.count.return_value=1
        page.get_by_role.return_value.nth.return_value.is_visible.return_value=True
        auth=MagicMock();auth.url='https://m.sbisec.co.jp/device';auth.get_by_role.return_value.count.return_value=1
        context=MagicMock();context.new_page.return_value=auth
        with patch('auto_login.recent_ids',return_value={'old'}),patch('auto_login.wait_message',return_value=('new','https://m.sbisec.co.jp/device')),patch('auto_login.unique_click') as click,patch('auto_login.visible',return_value=[]):
            device_auth(page,context,object(),'test@example.com')
            auth.get_by_role.return_value.fill.assert_called_once_with('123456')
            self.assertEqual([x.args[2][0] for x in click.call_args_list],['Eメールを送信する','認証する','デバイスを登録する'])
            auth.close.assert_called_once()
if __name__=='__main__':unittest.main()
