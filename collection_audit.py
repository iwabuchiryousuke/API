"""Durable per-document acquisition evidence, separate from ledger registration."""
import json,html,re
from datetime import datetime
from pathlib import Path

class Audit:
 def __init__(self,folder,account,items,expected=None,defer=False):
  self.defer=defer
  self.folder=Path(folder)/account;self.folder.mkdir(parents=True,exist_ok=True)
  self.stem='取得結果_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')
  self.data={'account':account,'expected':expected,'discovered':len(items),'note':'PDF保存と台帳登録は別です。星印は操作しません。画面件数には対象外書類が含まれる場合があります。','documents':[dict(date=i['date'],title=i['title'],status='未処理',file='',star='未処理',error='') for i in items]}
  self.flush()
 def update(self,index,**values):
  self.data['documents'][index].update(values)
  # Append one small record per document; render full report only at completion.
  with (self.folder/(self.stem+'.jsonl')).open('a',encoding='utf-8') as f:
   f.write(json.dumps({'index':index,**values},ensure_ascii=False)+'\n')
  if not self.defer:self.flush()
 def flush(self):
  p=self.folder/(self.stem+'.json');tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(self.data,ensure_ascii=False,indent=2),encoding='utf-8');tmp.replace(p)
  esc=lambda v:html.escape(str(v or ''))
  rows=''.join('<tr>'+''.join('<td>'+esc(row.get(k))+'</td>' for k in ('date','title','status','file','star','error'))+'</tr>' for row in self.data['documents'])
  body='<!doctype html><meta charset="utf-8"><title>PDF取得結果</title><style>body{font-family:sans-serif}table{border-collapse:collapse}td,th{border:1px solid #aaa;padding:8px}</style><h1>PDF取得結果</h1><p>'+esc(self.data['note'])+'</p><p>画面件数：'+esc(self.data['expected'] if self.data['expected'] is not None else '未確認')+' ／ 検出：'+str(self.data['discovered'])+'</p><table><tr><th>交付日</th><th>書類</th><th>PDF保存</th><th>ファイル</th><th>星印</th><th>理由・解析結果</th></tr>'+rows+'</table>'
  p=self.folder/(self.stem+'.html');tmp=p.with_suffix('.tmp');tmp.write_text(body,encoding='utf-8');tmp.replace(p)

