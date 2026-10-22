import unittest,tempfile
from pathlib import Path
from unittest.mock import Mock
from post_identity import verify,state,save_failure_evidence
from login_credentials import CredentialError

URL='https://plus.e-shishobako.ne.jp/dp_apl/usr/#/user-delivery'
class IdentityTests(unittest.TestCase):
 def page(self,values):
  p=Mock();p.url=URL;p.frames=[];p.locator.return_value.inner_text.side_effect=values;return p
 def test_exact_previous_race(self):
  # Old code failed immediately at the first empty render, before name appeared.
  values=['','読み込み中','岩渕\u3000俊吾\n様\n全82件']
  self.assertNotIn('岩渕俊吾様',values[0])
  p=self.page(values);log=[]
  verify(p,'岩渕俊吾',progress=log.append,clock=Mock(side_effect=[0,0,1]))
  self.assertEqual(p.wait_for_timeout.call_count,2)
  self.assertTrue(any('MATCH' in x for x in log));self.assertFalse(any('岩渕' in x for x in log))
 def test_wrong_account_stops_without_wait(self):
  p=self.page(['岩渕 将太 様'])
  with self.assertRaisesRegex(CredentialError,'選択口座以外'):verify(p,'岩渕俊吾')
  p.wait_for_timeout.assert_not_called()
 def test_zero_width_and_newlines(self):
  self.assertEqual(state('岩渕\u200b\u3000俊吾\n様','岩渕俊吾'),'MATCH')
 def test_timeout_not_reported_as_mismatch(self):
  p=self.page(['読み込み中'])
  with self.assertRaisesRegex(CredentialError,'別名義とは断定していません'):verify(p,'岩渕俊吾',timeout=0)
 def test_same_host_frame(self):
  p=self.page(['電子ポスト']);f=Mock();f.url=URL;f.locator.return_value.inner_text.return_value='岩渕俊吾 様';p.frames=[f];verify(p,'岩渕俊吾')
 def test_foreign_frame_not_accepted(self):
  p=self.page(['電子ポスト']);f=Mock();f.url='https://example.com/';f.locator.return_value.inner_text.return_value='岩渕俊吾 様';p.frames=[f]
  with self.assertRaises(CredentialError):verify(p,'岩渕俊吾',timeout=0)
  f.locator.assert_not_called()
 def test_body_loading_exception_retried(self):
  p=self.page([RuntimeError('frame reloading'),'岩渕俊吾 様']);verify(p,'岩渕俊吾',clock=Mock(side_effect=[0,0]))
 def test_conflicting_names_rejected(self):
  p=self.page(['岩渕俊吾 様 岩渕将太 様'])
  with self.assertRaises(CredentialError):verify(p,'岩渕俊吾')
 def test_screenshot_local_and_post_only(self):
  p=self.page([])
  with tempfile.TemporaryDirectory() as d:
   save_failure_evidence(p,Path(d),'A',lambda _:None);self.assertEqual(p.screenshot.call_count,1)
   p.url='https://login.sbisec.co.jp/otp/';save_failure_evidence(p,Path(d),'A',lambda _:None);self.assertEqual(p.screenshot.call_count,1)
if __name__=='__main__':unittest.main()
