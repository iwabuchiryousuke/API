import base64,unittest,json,sys,types
from unittest.mock import patch,MagicMock
from email.message import EmailMessage
from otp_parser import choose,visible_html,raw_payload,features
from gmail_auth import extract_with_raw,extract_code,ensure_service,service,SCOPES
from auto_login import otp_length,fill_otp
from test_v352 import message

class ParserTests(unittest.TestCase):
    def test_supported_layout_matrix(self):
        bodies=['認証コード：123456','【認証コード】\n123456','認証コードは以下になります。\n\n123456','認証コード\n\n「123456」','認 証 コ ー ド : 123456','認証番号は「123456」です','ワンタイムパスワード：123456','認証コード：１２３４５６','認証コード：12\u200b3456']
        for body in bodies:
            with self.subTest(layout=bodies.index(body)):self.assertEqual(choose(body),'123456')
    def test_html_inline_digits_table_hidden_script(self):
        text=visible_html('<style>認証コード:987654</style><table><tr><td>認証コード</td><td><b>123</b><b>456</b></td></tr></table>')
        self.assertEqual(choose(text),'123456')
    def test_alphanumeric(self):self.assertEqual(choose('認証コード：A1b2C3'),'A1b2C3')
    def test_screen_lengths(self):
        for length in range(4,13):
            code='1'*(length-1)+'2';self.assertEqual(choose('認証コード：'+code,length),code)
    def test_no_arbitrary_numbers(self):
        for text in ['口座番号 123456','認証コードを入力してください。電話123456','認証コード https://example.com/123456','認証コード：123456789','認証コード：abcdef']:
            self.assertIsNone(choose(text))
    def test_conflicting_codes(self):self.assertIsNone(choose('認証コード：123456\n認証コード：234567'))
    def test_duplicate_alternatives(self):self.assertEqual(choose('認証コード：123456\n認証コード：123456'),'123456')
    def test_raw_mime_quoted_printable(self):
        e=EmailMessage();e['From']='info@sbisec.co.jp';e['To']='test@example.com';e['Subject']='ログイン認証';e['Authentication-Results']='mx.google.com; dkim=pass header.i=@sbisec.co.jp;'
        e.set_content('認証コードは以下になります。\n123456',cte='quoted-printable')
        encoded=base64.urlsafe_b64encode(e.as_bytes()).decode()
        api=MagicMock();api.users().messages().get().execute.return_value={'raw':encoded}
        msg=message('');value,method=extract_with_raw(api,'id',msg,'test@example.com',100,extract_code)
        self.assertEqual((value,method),('123456','RAW'))
    def test_ambiguous_full_does_not_fall_back(self):
        api=MagicMock()
        value,method=extract_with_raw(api,'id',message('認証コード：123456\n認証コード：234567'),'test@example.com',100,extract_code)
        self.assertIsNone(value);self.assertEqual(method,'AMBIGUOUS');api.users.assert_not_called()
    def test_explanatory_sentence(self):
        self.assertEqual(choose('認証コードは、以下のとおりです。\n123456'),'123456')
    def test_expected_uppercase_only_code(self):
        self.assertEqual(choose('認証コード：ABCDXY',6),'ABCDXY')
        self.assertIsNone(choose('認証コード please',6))
    def test_raw_attachment_not_a_code(self):
        e=EmailMessage();e.set_content('認証コードはありません');e.add_attachment(b'123456',maintype='text',subtype='plain',filename='code.txt')
        p=raw_payload(base64.urlsafe_b64encode(e.as_bytes()).decode());self.assertNotIn('data',p['parts'][1].get('body',{}))
    def test_raw_cannot_bypass_authentication(self):
        e=EmailMessage();e['From']='info@sbisec.co.jp';e['To']='test@example.com';e['Subject']='ログイン認証';e.set_content('認証コード：123456')
        api=MagicMock();api.users().messages().get().execute.return_value={'raw':base64.urlsafe_b64encode(e.as_bytes()).decode()}
        value,method=extract_with_raw(api,'id',message(''),'test@example.com',100,extract_code)
        self.assertIsNone(value);self.assertEqual(method,'RAW_UNVERIFIED')
    def test_diagnostics_no_code(self):
        result=json.dumps(features('認証コード：123456'))
        self.assertNotIn('123456',result)
    def test_single_and_split_fields(self):
        f=MagicMock();f.get_attribute.return_value='8';self.assertEqual(otp_length([f]),8)
        fields=[MagicMock() for _ in range(8)];fill_otp(fields,'12345678')
        for i,field in enumerate(fields):field.fill.assert_called_once_with(str(i+1))

class TokenReuseTests(unittest.TestCase):
    def test_reuse_does_not_launch_consent(self):
        progress=[]
        with patch('gmail_auth.service',return_value=('api','mail')),patch('gmail_auth.connect') as connect:
            self.assertEqual(ensure_service('data','A',progress.append),('api','mail'));connect.assert_not_called()
        self.assertTrue(any('再利用' in x for x in progress))
    def test_valid_saved_token_no_refresh_no_consent(self):
        from family_profile import MAILBOXES
        modules={name:types.ModuleType(name) for name in ['google','google.oauth2','google.oauth2.credentials','google.auth','google.auth.transport','google.auth.transport.requests','googleapiclient','googleapiclient.discovery']}
        creds=MagicMock();creds.valid=True;creds.has_scopes.return_value=True
        factory=MagicMock();factory.from_authorized_user_info.return_value=creds
        modules['google.oauth2.credentials'].Credentials=factory;modules['google.auth.transport.requests'].Request=MagicMock()
        api=MagicMock();api.users().getProfile().execute.return_value={'emailAddress':MAILBOXES['B']}
        modules['googleapiclient.discovery'].build=MagicMock(return_value=api)
        setting={'recipient':MAILBOXES['B'],'credentials':{'refresh_token':'dummy'}}
        with patch.dict(sys.modules,modules),patch('gmail_auth.load',side_effect=lambda d,n:setting if n=='gmail_B' else None),patch('gmail_auth.save') as save:
            service('data','B');creds.refresh.assert_not_called();save.assert_called_once()

if __name__=='__main__':unittest.main()
