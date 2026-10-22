import unittest,tempfile,json,subprocess,io,shutil
from pathlib import Path
from unittest.mock import Mock,patch
from collection_audit import Audit
from epost_collector import collect,DISCOVER
from ledger import Store
from activity import Activities
from asset_snapshots import save,snapshots,capture_month_start
from report_model import build

class AcquisitionTests(unittest.TestCase):
 def test_report_survives_failure_and_escapes_html(self):
  with tempfile.TemporaryDirectory() as d:
   a=Audit(d,'D',[{'date':'2026/01/01','title':'<script>bad</script>'}],2)
   a.update(0,status='取得失敗',error='閲覧ボタンなし')
   self.assertIn('取得失敗',(a.folder/(a.stem+'.json')).read_text())
   self.assertNotIn('<script>',(a.folder/(a.stem+'.html')).read_text())
 def run_collect(self,d,fail=False):
  page=Mock();page.locator.return_value.inner_text.return_value='全1件';page.get_by_role.return_value.count.return_value=0
  item={'date':'2026/01/01','title':'取引報告書','key':'x','index':0}
  from pypdf import PdfWriter
  writer=PdfWriter();writer.add_blank_page(width=100,height=100);output=io.BytesIO();writer.write(output)
  result=Mock(side_effect=ValueError('PDF保存ボタンなし')) if fail else Mock(return_value=(output.getvalue(),0))
  with patch('epost_collector.discover_all',return_value=[item]),patch('epost_collector._retrieve',result),patch('epost_collector._resolve_index') as star,patch('pdf_batch.classify',return_value=('trade',None)) as parser:
   answer=collect(page,Mock(),'D',d,progress=lambda _:None)
   parser.assert_not_called()
   return answer,star.call_count
 def test_star_only_after_saved(self):
  with tempfile.TemporaryDirectory() as d:
   self.assertEqual(self.run_collect(d),((1,0,0),0));self.assertEqual(self.run_collect(d),((0,1,0),0))
 def test_failed_pdf_never_starred_and_audit_exists(self):
  with tempfile.TemporaryDirectory() as d:
   self.assertEqual(self.run_collect(d,True),((0,0,1),0))
   report=json.loads(next((Path(d)/'D').glob('取得結果_*.json')).read_text())
   self.assertEqual(report['documents'][0]['status'],'取得失敗')
 @unittest.skipUnless(shutil.which('node'), 'Node is optional for the legacy DOM fixture')
 def test_material_expand_preferred_over_star_in_actual_js(self):
  script="const parent={innerText:'2026/01/01 株式取引報告書 有効期限 2030/03/31',parentElement:null};const make=t=>({parentElement:parent,innerText:t,textContent:t,getAttribute:()=>null,getBoundingClientRect:()=>({width:20,height:20,right:100,top:20})});const nodes=[make('☆'),make('+')];global.window={scrollY:0};global.document={querySelectorAll:()=>nodes};console.log(JSON.stringify(("+DISCOVER+")()));"
  out=json.loads(subprocess.check_output(['node','-e',script],text=True));self.assertEqual(len(out),1);self.assertEqual(out[0]['index'],1)

class AssetTests(unittest.TestCase):
 def test_monthly_difference_never_creates_a_first_day_snapshot(self):
  with tempfile.TemporaryDirectory() as d:
   st=Store(d)
   try:
    rows=[['商品','評価額','前月比(率)'],['国内株式','1,100円','+100円\n+10%'],['預り金(円)','300円','0円'],['スィープ専用銀行口座','400円','+50円']]
    save(st,'A','2026-09-30',{'cash':650},note='以前の確認記録')
    before=snapshots(st)
    self.assertEqual(capture_month_start(st,'A','2026/10/21 19:00',[rows]),0)
    self.assertEqual(snapshots(st),before)
    self.assertNotIn(('A','2026-10-01'),snapshots(st))
   finally:st.close()
 def test_requested_columns_and_exact_nisa_dates(self):
  with tempfile.TemporaryDirectory() as d:
   st=Store(d)
   try:
    save(st,'A','2026-10-01',{'domestic':100,'foreign':200,'cash':300,'foreign_cash':400,'growth_remaining':500,'tsumitate_remaining':600})
    m=build(st,Activities(st),'2026-10');cells={(c['r'],c['c']):c for c in m['sheets'][0]['cells']}
    self.assertEqual(cells[1,18]['v'],'成長枠残高');self.assertEqual(cells[1,19]['v'],'積立枠残高');self.assertEqual(cells[1,20]['v'],'確認状態')
    self.assertEqual(cells[12,18]['v'],500);self.assertIsNone(cells[2,18]['v']);self.assertEqual(cells[12,15]['v'],300)
   finally:st.close()
if __name__=='__main__':unittest.main()

class ViewerSelectionTests(unittest.TestCase):
 def test_truncated_pdf_is_rejected_without_files(self):
  # Behavioral safety replaces the old AST-equality assertion, which locked in
  # the very collector bugs this release fixes.
  from epost_collector import save_pdf
  with tempfile.TemporaryDirectory() as d:
   with self.assertRaises(ValueError):save_pdf(d,'A','2026/01/01','取引報告書',b'%PDF-test')
   self.assertEqual(list(Path(d).rglob('*')),[])
 def test_deferred_audit_journals_without_rewriting(self):
  with tempfile.TemporaryDirectory() as d:
   a=Audit(d,'A',[{'date':'2026/01/01','title':'取引報告書'}],defer=True)
   p=a.folder/(a.stem+'.html');before=p.read_bytes()
   a.update(0,status='新規保存')
   self.assertEqual(p.read_bytes(),before)
   self.assertIn('新規保存',(a.folder/(a.stem+'.jsonl')).read_text())
   a.flush();self.assertIn('新規保存',p.read_text())
