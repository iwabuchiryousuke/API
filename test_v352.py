import unittest,base64
from types import SimpleNamespace
from unittest.mock import MagicMock,patch
from gmail_auth import extract_code,message_reason,wait_message,GmailError
from login_credentials import CredentialError
from epost_collector import _login_page,autofill_login

def message(body='認証コード：123456',recipient='test@example.com',auth='mx.google.com; dkim=pass header.i=@sbisec.co.jp;',date='200000',mime='text/plain'):
    return {'internalDate':date,'payload':{'mimeType':mime,'headers':[{'name':'From','value':'info@sbisec.co.jp'},{'name':'To','value':recipient},{'name':'Subject','value':'ログイン認証'},{'name':'Authentication-Results','value':auth}],'body':{'data':base64.urlsafe_b64encode(body.encode()).decode()}}}
class MailTests(unittest.TestCase):
    def test_html_and_full_width(self):
        m=message('認証コード：<b>１２３４５６</b>',mime='text/html')
        self.assertEqual(extract_code(m,'test@example.com',100),'123456')
    def test_quoted_label(self):
        self.assertEqual(extract_code(message('【認証コード】\n123456'),'test@example.com',100),'123456')
    def test_multiple_codes_rejected(self):
        self.assertIsNone(extract_code(message('認証コード:123456 認証コード:234567'),'test@example.com',100))
    def test_reason_separates_missing_and_mismatch(self):
        self.assertEqual(message_reason(message(recipient='other@example.com'),'test@example.com',100),'RECIPIENT')
        self.assertEqual(message_reason(message(auth='mx.google.com; dkim=fail'),'test@example.com',100),'AUTHENTICITY')
        self.assertEqual(message_reason(message(date='1'),'test@example.com',100),'OLD')
    def test_success_ignores_old_ids(self):
        api=MagicMock();api.users().messages().list().execute.return_value={'messages':[{'id':'old'},{'id':'new'}]}
        api.users().messages().get().execute.return_value=message()
        self.assertEqual(wait_message(api,'test@example.com',100,extract_code,{'old'},timeout=2),('new','123456'))
    def test_diagnostic_never_contains_content(self):
        api=MagicMock();api.users().messages().list().execute.return_value={'messages':[{'id':'new'}]}
        api.users().messages().get().execute.return_value=message('private-body 123456',recipient='other@example.com')
        log=[]
        with patch('gmail_auth.time.monotonic',side_effect=[0,0,4]),patch('gmail_auth.time.sleep'):
            with self.assertRaises(CredentialError) as error:wait_message(api,'test@example.com',100,extract_code,timeout=3,progress=log.append)
        self.assertIn('宛先不一致',str(error.exception));self.assertNotIn('123456',str(error.exception));self.assertNotIn('private-body',''.join(log))
    def test_cancel_before_api(self):
        api=MagicMock()
        with self.assertRaises(CredentialError):wait_message(api,'test@example.com',100,extract_code,cancelled=lambda:True)
        api.users.assert_not_called()
    def test_two_candidates_stop(self):
        api=MagicMock();api.users().messages().list().execute.return_value={'messages':[{'id':'one'},{'id':'two'}]};api.users().messages().get().execute.return_value=message()
        with self.assertRaisesRegex(CredentialError,'複数'):wait_message(api,'test@example.com',100,extract_code)

class LoginTests(unittest.TestCase):
    def frame(self,url,fields):
        f=MagicMock();f.url=url;f.evaluate.return_value=fields;return f
    def context(self,frames):
        p=MagicMock();p.is_closed.return_value=False;p.frames=frames
        return SimpleNamespace(pages=[p])
    def test_login_inside_trusted_frame(self):
        outer=self.frame('https://www.sbisec.co.jp/',None);inner=self.frame('https://m.sbisec.co.jp/login',{'username':0,'password':1})
        self.assertIs(_login_page(self.context([outer,inner])),inner)
    def test_foreign_frame_never_read(self):
        f=self.frame('https://sbisec.co.jp.evil.example/',{'username':0,'password':1})
        self.assertIsNone(_login_page(self.context([f])));f.evaluate.assert_not_called()
    def test_duplicate_forms_stop(self):
        a=self.frame('https://www.sbisec.co.jp/',{'username':0,'password':1});b=self.frame('https://m.sbisec.co.jp/',{'username':0,'password':1})
        with self.assertRaises(CredentialError):_login_page(self.context([a,b]))
    def test_delayed_controls_retried_before_filling(self):
        page=MagicMock();target=self.frame('https://m.sbisec.co.jp/login',{'username':0,'password':1})
        with patch('epost_collector.load_credentials',return_value=('dummy-user','dummy-password')),patch('epost_collector._login_page',side_effect=[None,None,target]),patch('epost_collector._open_login',side_effect=[False,True]) as open_login:
            self.assertTrue(autofill_login(page,object(),'A',automatic=True))
            self.assertEqual(open_login.call_count,2)
        self.assertEqual([c.args[0] for c in target.locator().nth().fill.call_args_list],['dummy-user','dummy-password'])

if __name__=='__main__':unittest.main()
