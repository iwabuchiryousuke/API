"""Screenshot-derived layout reproduction using synthetic, non-live codes."""
import unittest,base64
from unittest.mock import MagicMock,patch
from email.message import EmailMessage
from otp_parser import choose,visible_html
from gmail_auth import wait_code,extract_code,extract_with_raw
from auto_login import fill_otp,unique_click
from test_v352 import message

HEADING='ログイン時の認証に必要な認証コードはこちら'
def layout(code='135790',depth=4,indent=80):
    return ('<h2>【SBI証券】認証コードのお知らせ（ログイン時の追加認証）</h2>'
      '<p>当社WEBサイトにて認証コード入力画面を表示しております。</p>'
      '<p>下記の認証コードを当社WEBサイトの認証コード入力欄に入力してください。</p>'
      '<h2>'+HEADING+'</h2>'+('\n'+' '*indent+'<table><tr><td><div>\n')*depth+
      '<span>'+code+'</span>'+'\n</div></td></tr></table>'*depth+
      '<p>※5分経過すると認証コードは無効となります。</p>'
      '<p>以下の流れで認証コードの入力が要求されました。認証コード入力画面</p>'
      '<p>認証コードを入力してください。お問い合わせ 0800-170-5333</p>')

class LayoutTests(unittest.TestCase):
    def test_screenshot_heading_with_nested_tables(self):
        for depth in (1,4,12):
            for indent in (0,80,500):
                with self.subTest(depth=depth,indent=indent):
                    self.assertEqual(choose(visible_html(layout(depth=depth,indent=indent))),'135790')
    def test_layout_only_whitespace_never_changes_code(self):
        for blank in ('\n'*30,' '*3000,'\t\n '*400,'\u3000\r\n'*100):
            self.assertEqual(choose(HEADING+blank+'135790'),'135790')
    def test_unrelated_prose_still_stops_scan(self):
        for body in (HEADING+'\n'*20+'お問い合わせ番号 135790',HEADING+'\n'*20+'https://example.com/135790',HEADING+'\n'*20+'有効期限は135790'):
            self.assertIsNone(choose(body))
    def test_two_codes_still_stop(self):
        self.assertIsNone(choose(visible_html(layout()+layout('246802'))))
    def test_all_four_account_mail_to_fill_and_submit(self):
        from family_profile import MAILBOXES
        for account in 'ABCD':
            api=MagicMock();api.users().messages().list().execute.return_value={'messages':[{'id':'new'}]}
            api.users().messages().get().execute.return_value=message(layout(),recipient=MAILBOXES[account],mime='text/html')
            with patch('gmail_auth.time.sleep'):
                _,code=wait_code(api,MAILBOXES[account],100,seen={'old'},length=6)
            field=MagicMock();fill_otp([field],code)
            field.fill.assert_called_once_with('135790')
            page=MagicMock();page.url='https://m.sbisec.co.jp/login'
            page.get_by_role.return_value.count.return_value=1
            page.get_by_role.return_value.nth.return_value.is_visible.return_value=True
            unique_click(page,'button',['認証する'])
            page.get_by_role.return_value.nth.return_value.click.assert_called_once()
            api.users().messages().get.assert_called_with(userId='me',id='new',format='full')
    def test_full_and_raw_both_use_fixed_layout(self):
        raw=EmailMessage();raw['From']='info@sbisec.co.jp';raw['To']='test@example.com';raw['Subject']='認証コードのお知らせ（ログイン時の追加認証）';raw['Authentication-Results']='mx.google.com; dkim=pass header.i=@sbisec.co.jp;'
        raw.set_content(layout(),subtype='html',cte='quoted-printable')
        api=MagicMock();api.users().messages().get().execute.return_value={'raw':base64.urlsafe_b64encode(raw.as_bytes()).decode()}
        code,method=extract_with_raw(api,'new',message(''),'test@example.com',100,extract_code)
        self.assertEqual((code,method),('135790','RAW'))
    def test_matching_body_wrong_owner_never_filled(self):
        self.assertIsNone(extract_code(message(layout(),recipient='other@example.com',mime='text/html'),'test@example.com',100))
    def test_matching_body_old_email_never_filled(self):
        self.assertIsNone(extract_code(message(layout(),mime='text/html'),'test@example.com',201))

if __name__=='__main__':unittest.main()
