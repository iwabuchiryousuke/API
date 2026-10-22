import unittest,json,tempfile
from unittest.mock import MagicMock
from sbi_views import parse_summary,parse_my_assets,verify_post_owner,landing,latest,save_observation
from jquants_dividends import active_notices,last_rights,fetch,get_rows,MarketError
from login_credentials import CredentialError
from ledger import Store,LedgerError

SUMMARY='''投資可能枠(2026年) - NISA(成長投資枠)
0
投資可能枠(2026年) - NISA(つみたて投資枠)
1,200,000
現金残高等(合計)
335,709
SBI証券口座分
0
スィープ専用銀行口座
335,709
買付余力
999,999'''
def notice(ref='a',**kw):
 r=dict(Code='12340',RefNo=ref,CARefNo=ref,StatCode='1',PubDate='2026-01-01',PubTime='12:00',RecDate='2026-09-30',ExDate='2026-09-29',IFCode='2',FRCode='2',DivRate='10',PayDate='2027-01-05');r.update(kw);return r
class Views(unittest.TestCase):
 def test_nisa_and_cash(self):
  r=parse_summary(SUMMARY);self.assertEqual(r['growth_remaining']['remaining'],0);self.assertEqual(r['tsumitate_remaining']['remaining'],1200000);self.assertEqual(r['cash_total'],335709);self.assertEqual(r['broker_cash'],0)
 def test_duplicate_cash(self):
  with self.assertRaises(LedgerError):parse_summary(SUMMARY+'\n現金残高等(合計)\n100')
 def test_my_assets(self):
  r=parse_my_assets('国内株式(現物)\n8,316,780円\n+3,995,540円\nスィープ専用銀行口座\n335,709円',[])
  self.assertEqual(r['domestic'],8316780);self.assertEqual(r['bank'],335709);self.assertNotIn('foreign',r)
 def test_no_dollar_as_yen(self):
  r=parse_my_assets('国内株式(現物)\n100円\n米国株式\n100 USD',[]);self.assertIsNone(r.get('foreign'))
 def test_owner_match_and_mismatch(self):
  p=MagicMock();p.url='https://plus.e-shishobako.ne.jp/dp_apl/usr/#/user-delivery';p.locator().inner_text.return_value='架空 太郎 様'
  verify_post_owner(p,'架空太郎')
  with self.assertRaises(CredentialError):verify_post_owner(p,'架空次郎',timeout=0)
  p.url='https://example.com/'
  with self.assertRaises(CredentialError):verify_post_owner(p,'架空太郎')
 def test_no_name_home_can_navigate_only(self):
  p=MagicMock();f=MagicMock();f.url='https://site3.sbisec.co.jp/ETGate/';f.locator().inner_text.return_value='ログアウト 口座管理';p.frames=[f];c=MagicMock();c.pages=[p]
  self.assertIs(landing(c),p)
 def test_observations_not_backdated(self):
  with tempfile.TemporaryDirectory() as d:
   s=Store(d)
   try:
    save_observation(s,'B','口座サマリー',SUMMARY,parse_summary(SUMMARY))
    self.assertEqual(latest(s,'2000-01-01'),{});self.assertIn(('B','口座サマリー'),latest(s,'2099-01-01'))
   finally:s.close()
class CommitTests(unittest.TestCase):
 def test_collected_observation_survives_review_and_commit(self):
  from update_workflow import Workflow
  from pathlib import Path
  with tempfile.TemporaryDirectory() as directory:
   base=Path(directory)
   def collect(account,store):
    save_observation(store,account,'口座サマリー',SUMMARY,parse_summary(SUMMARY))
    return {'complete':True,'assets_observed':True}
   Workflow(base).run(collect=collect,review=lambda *a:True,accounts=['B'])
   s=Store(base/'data')
   try:self.assertEqual(latest(s,'2099-01-01')[('B','口座サマリー')]['values']['cash_total'],335709)
   finally:s.close()
 def test_cancel_does_not_commit_observation(self):
  from update_workflow import Workflow,Cancelled
  from pathlib import Path
  with tempfile.TemporaryDirectory() as directory:
   base=Path(directory)
   def collect(account,store):
    save_observation(store,account,'口座サマリー',SUMMARY,parse_summary(SUMMARY))
    return {'complete':True,'assets_observed':True}
   with self.assertRaises(Cancelled):Workflow(base).run(collect=collect,review=lambda *a:False,accounts=['B'])
   s=Store(base/'data')
   try:self.assertEqual(latest(s,'2099-01-01'),{})
   finally:s.close()
class Dividends(unittest.TestCase):
 def test_correction(self):
  rows=[notice(),notice('b',StatCode='2',CARefNo='a',PubDate='2026-02-01',DivRate='20')]
  self.assertEqual(active_notices(rows,'1234')[0]['DivRate'],'20')
 def test_delete_correction_chain(self):
  rows=[notice(),notice('b',StatCode='2',CARefNo='a',PubDate='2026-02-01'),notice('c',StatCode='3',CARefNo='b',PubDate='2026-03-01')]
  self.assertEqual(active_notices(rows,'1234'),[])
 def test_decided_not_added_to_forecast(self):
  rows=[notice(),notice('b',FRCode='1',PubDate='2026-02-01',DivRate='20')];self.assertEqual(len(active_notices(rows,'1234')),1)
 def test_holiday_rights(self):
  rows=[{'Date':'2026-09-28','HolDiv':'3'},{'Date':'2026-09-27','HolDiv':'0'},{'Date':'2026-09-26','HolDiv':'0'},{'Date':'2026-09-25','HolDiv':'1'}]
  self.assertEqual(last_rights('2026-09-29',rows),'2026-09-25');self.assertIsNone(last_rights('2026-09-29',[]))
 def test_next_year_payment_kept(self):
  def req(path,params,key):return [notice()] if path=='fins/dividend' else [{'Date':'2026-09-28','HolDiv':'1'}]
  f=fetch('1234',2026,'dummy',req);self.assertEqual(f['events'][0]['payment'],'2027-01-05');self.assertFalse(f['calendar_complete']);self.assertIsNone(f['dps'])
 def test_unknown_dps(self):
  def req(path,params,key):return [notice(DivRate='-')] if path=='fins/dividend' else []
  self.assertIsNone(fetch('1234',2026,'dummy',req)['events'][0]['dps'])
 def test_pagination_and_cycle(self):
  import io
  calls=[]
  def opener(req,timeout):
   calls.append(req.full_url);return io.StringIO(json.dumps({'data':[{}],'pagination_key':'p'} if len(calls)==1 else {'data':[{}]}))
  self.assertEqual(len(get_rows('fins/dividend',{'code':'1234'},'dummy',opener)),2)
  def cycle(req,timeout):return io.StringIO('{"data":[],"pagination_key":"p"}')
  with self.assertRaises(MarketError):get_rows('fins/dividend',{},'dummy',cycle)
if __name__=='__main__':unittest.main()
