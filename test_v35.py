import unittest,json,tempfile,sys,types
from pathlib import Path
from unittest.mock import patch,MagicMock
from datetime import date
from zipfile import ZipFile
import xml.etree.ElementTree as ET
import test_regression as fixture
from gmail_auth import connect,client_config,verify_mailbox,ensure_service,GmailError,explain
from family_profile import OWNERS,MAILBOXES,configure
from date_highlights import dates_in,months_after,rules_for
from standalone_excel import write_model
from report_model import build
from update_workflow import Workflow,UpdateError
from login_credentials import CredentialError

class GmailTests(unittest.TestCase):
    def test_wrong_google_account_rejected(self):
        api=MagicMock();api.users().getProfile().execute.return_value={'emailAddress':MAILBOXES['A']}
        with self.assertRaisesRegex(GmailError,'MAILBOX_MISMATCH'):verify_mailbox(api,MAILBOXES['B'])
    def test_reuse_client_not_A_token(self):
        old={'credentials':{'client_id':'test.apps.googleusercontent.com','client_secret':'test','refresh_token':'do-not-copy'}}
        with patch('gmail_auth.load',side_effect=lambda d,n:old if n=='gmail_A' else None),patch('gmail_auth.save'):
            value=client_config(Path('data'))
        self.assertNotIn('refresh_token',json.dumps(value));self.assertEqual(value['installed']['client_id'],'test.apps.googleusercontent.com')
    def test_auth_each_target_and_profile_before_save(self):
        flow=MagicMock();credentials=MagicMock();credentials.refresh_token='test-token';credentials.to_json.return_value='{}';credentials.has_scopes.return_value=True
        flow.run_local_server.return_value=credentials
        factory=MagicMock();factory.from_client_config.return_value=flow
        api=MagicMock();module_flow=types.ModuleType('google_auth_oauthlib.flow');module_flow.InstalledAppFlow=factory
        module_discovery=types.ModuleType('googleapiclient.discovery');module_discovery.build=MagicMock(return_value=api)
        modules={'google_auth_oauthlib':types.ModuleType('google_auth_oauthlib'),'google_auth_oauthlib.flow':module_flow,'googleapiclient':types.ModuleType('googleapiclient'),'googleapiclient.discovery':module_discovery}
        with patch.dict(sys.modules,modules),patch('gmail_auth.client_config',return_value={}),patch('gmail_auth.save') as save:
            for a in 'ABCD':
                api.users().getProfile().execute.return_value={'emailAddress':MAILBOXES[a]}
                connect(Path('data'),a)
                self.assertEqual(flow.run_local_server.call_args.kwargs['login_hint'],MAILBOXES[a])
                self.assertEqual(save.call_args.args[1],'gmail_'+a)
            save.reset_mock();api.users().getProfile().execute.return_value={'emailAddress':MAILBOXES['A']}
            with self.assertRaisesRegex(GmailError,'MAILBOX_MISMATCH'):connect(Path('data'),'D')
            save.assert_not_called()
    def test_permission_repair_does_not_loop_network_errors(self):
        with patch('gmail_auth.service',side_effect=GmailError('NEEDS_CONSENT','x')),patch('gmail_auth.connect',return_value=('api','mail')) as connect_mock:
            self.assertEqual(ensure_service(Path('data'),'B'),('api','mail'));connect_mock.assert_called_once()
        with patch('gmail_auth.service',side_effect=GmailError('API_FORBIDDEN','x')),patch('gmail_auth.connect') as connect_mock:
            with self.assertRaises(GmailError):ensure_service(Path('data'),'B')
            connect_mock.assert_not_called()
    def test_safe_diagnostics(self):
        e=ValueError('sensitive URL or token');self.assertNotIn(str(e),str(explain(e)))
        e.error='access_denied';self.assertEqual(explain(e).code,'ACCESS_DENIED')

class ExportTests(unittest.TestCase):
    setUp=fixture.Regression.setUp;tearDown=fixture.Regression.tearDown;trade=fixture.Regression.trade
    def test_fixed_identity(self):
        configure(self.store)
        self.assertEqual({x['id']:x['owner_name'] for x in self.store.accounts()},OWNERS)
    def test_date_and_year_styles_not_gain_style(self):
        self.trade();m=build(self.store,self.a,'2026-10')
        p=write_model(m,Path(self.tmp.name)/'out')
        ns={'x':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        with ZipFile(p) as z:
            styles=ET.fromstring(z.read('xl/styles.xml'));formats={x.attrib['numFmtId']:x.attrib['formatCode'] for x in styles.find('x:numFmts',ns)};xfs=list(styles.find('x:cellXfs',ns))
            t=ET.fromstring(z.read('xl/worksheets/sheet1.xml'));d=ET.fromstring(z.read('xl/worksheets/sheet2.xml'))
            for ref in ('L1','L11'):
                c=t.find(f'.//x:c[@r="{ref}"]',ns);self.assertEqual(formats[xfs[int(c.attrib['s'])].attrib['numFmtId']],'m/d');self.assertIsNotNone(c.find('x:v',ns))
            years=[c for c in m['sheets'][1]['cells'] if c.get('format')=='[Black]0"年"'];self.assertTrue(years)
            for cell in years:
                c=d.find(f'.//x:c[@r="M{cell["r"]}"]',ns);self.assertEqual(formats[xfs[int(c.attrib['s'])].attrib['numFmtId']],'[Black]0"年"')
    def test_account_boundary_all_columns(self):
        self.trade(account='A');self.trade(account='B',day='2026-01-03')
        m=build(self.store,self.a);t=m['sheets'][0]
        border=[c for c in t['cells'] if c.get('border_top')]
        self.assertEqual(len(border),12);self.assertEqual({c['c'] for c in border},set(range(22,34)))
    def test_multiple_dates_and_year_boundary(self):
        self.assertEqual(dates_in('2026/03/27・09/28'),[date(2026,3,27),date(2026,9,28)])
        self.assertEqual(months_after(date(2026,1,31),1),date(2026,2,28))
        self.assertEqual(dates_in('未確認'),[])
        rules=rules_for({'v':'2026/10/30','r':2,'c':14})
        self.assertIn('TODAY()',rules[0]['formula']);self.assertIn('NOT(',rules[1]['formula']);self.assertEqual(rules[0]['range'],'N2')
    def test_failed_account_does_not_skip_later_accounts(self):
        called=[]
        def collect(a,store):
            called.append(a)
            if a=='B':raise CredentialError('mock failure')
            return {'complete':True,'assets_observed':False}
        flow=Workflow(Path(self.tmp.name))
        with self.assertRaises(UpdateError):flow.run(collect=collect,review=lambda *args:True)
        self.assertEqual(called,list('ABCD'));self.assertFalse(flow.committed)
    def test_provision_invalid_key_leaves_existing_credentials(self):
        from cryptography.fernet import Fernet
        from initial_setup import install
        root=Path(self.tmp.name);(root/'personal_setup.enc').write_bytes(Fernet(Fernet.generate_key()).encrypt(b'{}'))
        with patch('initial_setup.save_credentials') as save:
            with self.assertRaises(CredentialError):install(root,Fernet.generate_key().decode())
            save.assert_not_called()

if __name__=='__main__':unittest.main()
