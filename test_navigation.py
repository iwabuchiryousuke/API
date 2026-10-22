import unittest
from unittest.mock import Mock,patch
from sbi_navigation import open_post,open_post_resilient,controls
from login_credentials import CredentialError

def row(label,index=0,domestic=False,href=''):
 return dict(labels=[label],index=index,domestic=domestic,href=href,visible=True,enabled=True)
def page():
 p=Mock();p.url='https://m.sbisec.co.jp/account';p.is_closed.return_value=False;p.frames=[p];return p
class Navigation(unittest.TestCase):
 def test_single_roundtrip_hundreds_links(self):
  p=page();p.locator.return_value.evaluate_all.return_value=[row('リンク'+str(i),i) for i in range(800)]+[row('取引報告書等（電子交付）',800)]
  self.assertEqual(len(controls(p,['取引報告書等(電子交付)'])),1)
  p.locator.return_value.evaluate_all.assert_called_once();p.locator.return_value.count.assert_not_called()
 def test_image_alt_label_and_disabled(self):
  p=page();r=row('報告書閲覧',domestic=True);r['labels']=['', '報告書閲覧'];p.locator.return_value.evaluate_all.return_value=[r]
  self.assertEqual(len(controls(p,['報告書閲覧'],True)),1)
  r['enabled']=False;self.assertEqual(controls(p,['報告書閲覧'],True),[])
 def test_tabs_frames(self):
  p=page();other=page();ctx=Mock(pages=[p,other]);post=page();post.url='https://plus.e-shishobako.ne.jp/dp_apl/usr/#/user-delivery'
  p.locator.return_value.evaluate_all.return_value=[]
  other.locator.return_value.evaluate_all.return_value=[row('報告書閲覧',domestic=True)]
  other.locator.return_value.nth.return_value.click.side_effect=lambda **k:ctx.pages.append(post)
  with patch('epost_collector.wait_for_post_tab',return_value=post):self.assertIs(open_post(p,ctx),post)
  other.locator.return_value.nth.return_value.click.assert_called_once()
 def test_menu_render_does_not_click_back_to_account(self):
  p=page();ctx=Mock(pages=[p]);state=[0];post=page();post.url='https://plus.e-shishobako.ne.jp/dp_apl/usr/#/user-delivery'
  def scan():
   if state[0]==0:return [row('口座管理',1),row('取引報告書等（電子交付）',2)]
   if state[0]<3:return [row('口座管理',1),row('取引報告書等（電子交付）',2)]
   return [row('報告書一覧',3,True)]
  p.locator.return_value.evaluate_all.side_effect=lambda _:scan()
  clicks=[]
  def nth(i):
   c=Mock()
   def click(**kw):
    clicks.append(i)
    if i==2:state[0]=1
    if i==3:ctx.pages.append(post)
   c.click.side_effect=click;return c
  p.locator.return_value.nth.side_effect=nth
  p.wait_for_timeout.side_effect=lambda _:state.__setitem__(0,state[0]+1)
  with patch('epost_collector.wait_for_post_tab',return_value=post):self.assertIs(open_post(p,ctx),post)
  self.assertEqual(clicks,[2,3])
 def test_ambiguous_reports_never_clicked(self):
  p=page();ctx=Mock(pages=[p]);p.locator.return_value.evaluate_all.return_value=[row('報告書閲覧',1,True),row('報告書閲覧',2,True)]
  with self.assertRaises(CredentialError):open_post(p,ctx,clock=Mock(side_effect=[0,0,31]))
  p.locator.return_value.nth.assert_not_called()
 def test_untrusted_frame_ignored(self):
  p=page();p.url='https://example.com/';ctx=Mock(pages=[p])
  with self.assertRaises(CredentialError):open_post(p,ctx,clock=Mock(side_effect=[0,0,31]))
  p.locator.assert_not_called()
 def test_manual_recovery_uses_existing_session(self):
  p=page();ctx=Mock(pages=[p]);post=page();post.url='https://plus.e-shishobako.ne.jp/dp_apl/usr/#/user-delivery'
  p.wait_for_timeout.side_effect=lambda _:ctx.pages.append(post)
  with patch('sbi_navigation.open_post',side_effect=CredentialError('route missing')),patch('epost_collector.wait_for_post_tab',return_value=post):
   self.assertIs(open_post_resilient(p,ctx),post)
  p.bring_to_front.assert_called_once();ctx.close.assert_not_called()
 def test_cancellation_does_not_enter_manual_wait(self):
  p=page()
  with self.assertRaisesRegex(CredentialError,'中止'):open_post_resilient(p,Mock(pages=[p]),cancelled=lambda:True)
  p.bring_to_front.assert_not_called()
 def test_manual_timeout_is_failure_not_success(self):
  p=page()
  with patch('sbi_navigation.open_post',side_effect=CredentialError('missing')):
   with self.assertRaisesRegex(CredentialError,'待機時間内'):open_post_resilient(p,Mock(pages=[p]),clock=Mock(side_effect=[0,181]))

class WorkflowRecovery(unittest.TestCase):
 def test_pdf_saved_before_asset_failure_and_owner_checked(self):
  from contextlib import ExitStack
  from auto_login import run_one
  events=[];p=page();p.locator.return_value.inner_text.return_value='岩渕亮介 様 ログアウト 口座管理'
  p.locator.return_value.count.return_value=0;p.locator.return_value.evaluate_all.return_value=[]
  pw=Mock();browser=pw.chromium.launch.return_value;ctx=browser.new_context.return_value
  ctx.new_page.return_value=p;ctx.pages=[p]
  p.context=ctx
  st=Mock();st.accounts.return_value=[{'id':'C','owner_name':'岩渕亮介'}]
  with ExitStack() as stack:
   patches={
    'auto_login.Store':Mock(return_value=st),
    'auto_login.autofill_login':Mock(return_value=True),
    'auto_login._login_page':Mock(return_value=p),
    'auto_login.recent_ids':Mock(return_value=set()),
    'auto_login.submit_login':Mock(),
    'auto_login.visible':Mock(return_value=[]),
    'auto_login.otp_fields':Mock(return_value=[]),
    'sbi_views.landing':Mock(return_value=p),
    'auto_login.open_electronic_post':Mock(return_value=p),
    'sbi_views.verify_post_owner':Mock(side_effect=lambda *a,**kw:events.append('owner')),
    'auto_login.collect':Mock(side_effect=lambda *a,**kw:events.append('pdf') or (2,0,0)),
    'sbi_views.capture_account':Mock(side_effect=lambda *a,**kw:events.append('assets') or (_ for _ in ()).throw(ValueError('missing assets'))),
   }
   for name,mock in patches.items():stack.enter_context(patch(name,mock))
   result=run_one(pw,'C',asset_store=st,gmail_session=(Mock(),'test@example.com'))
  self.assertEqual(events,['owner','pdf','assets'])
  self.assertTrue(result['complete']);self.assertFalse(result['assets_observed']);self.assertEqual(result['saved'],2)
  browser.close.assert_called_once()
