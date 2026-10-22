import unittest
from unittest.mock import MagicMock,patch
from auto_login import submit_otp,auth_label
from login_credentials import CredentialError

def control(label='認証する',visible=True,enabled=True):
    c=MagicMock();c.inner_text.return_value=label;c.get_attribute.return_value=None
    c.is_visible.return_value=visible;c.is_enabled.return_value=enabled
    return c

def fixture(items,form=True):
    page=MagicMock();page.url='https://login.sbisec.co.jp/otp/entry'
    field=MagicMock();f=field.locator.return_value;f.count.return_value=int(form)
    loc=(f if form else page).locator.return_value
    loc.count.return_value=len(items);loc.nth.side_effect=lambda i:items[i]
    return page,field

class SubmitTests(unittest.TestCase):
    def test_labels(self):
        for label in ('認証する',' 認証する ›','認 証 す る\n>','認証する →'):
            self.assertEqual(auth_label(label),'認証する')
        self.assertNotEqual(auth_label('認証コードを再送する'),'認証する')
    def test_form_scoped_arrow(self):
        c=control('認証する ›');p,f=fixture([c]);log=[]
        submit_otp(p,[f],log.append,timeout=0)
        c.click.assert_called_once();f.press.assert_called_once_with('Tab');p.locator.assert_not_called()
        self.assertEqual(len(log),2)
    def test_input_value_and_link_labels(self):
        for attr in ('value','aria-label','alt'):
            c=control('');c.get_attribute.side_effect=lambda k:'認証する' if k==attr else None
            p,f=fixture([c],form=False);submit_otp(p,[f],timeout=0);c.click.assert_called_once()
    def test_duplicate_never_clicks(self):
        a,b=control(),control();p,f=fixture([a,b])
        with self.assertRaises(CredentialError):submit_otp(p,[f],timeout=0)
        a.click.assert_not_called();b.click.assert_not_called()
    def test_disabled_never_clicks(self):
        c=control(enabled=False);p,f=fixture([c])
        with self.assertRaisesRegex(CredentialError,'有効=False'):submit_otp(p,[f],timeout=0)
        c.click.assert_not_called()
    def test_wait_for_enabled(self):
        c=control();c.is_enabled.side_effect=[False,True];p,f=fixture([c])
        with patch('auto_login.time.monotonic',side_effect=[0,0]):submit_otp(p,[f],timeout=1)
        p.page.wait_for_timeout.assert_called_once();c.click.assert_called_once()
    def test_no_candidate_or_hidden_or_resend(self):
        for items in ([],[control(visible=False)],[control('認証コードを再送する')]):
            p,f=fixture(items)
            with self.assertRaisesRegex(CredentialError,'表示候補=0'):submit_otp(p,[f],timeout=0)
            for c in items:c.click.assert_not_called()
    def test_untrusted_never_clicks(self):
        c=control();p,f=fixture([c]);p.url='https://example.com/otp'
        with self.assertRaises(CredentialError):submit_otp(p,[f],timeout=0)
        c.click.assert_not_called();f.press.assert_not_called()
if __name__=='__main__':unittest.main()
