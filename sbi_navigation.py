"""Read-only navigation using observed controls, including SSO popup redirects.

Do not navigate to invented SBI endpoints or follow an unrelated external link.
Collection still requires the separate electronic-post owner check.
"""
import time
from urllib.parse import urlparse, urljoin
from login_credentials import CredentialError
from sbi_views import norm,frames

SELECTOR='a,button,input[type="button"],input[type="submit"],input[type="image"],[role="button"]'
# One browser round trip, rather than four round trips for every link on a page.
SCAN=r'''els=>{
 const prefix=Date.now().toString(36)+'-'+Math.random().toString(36).slice(2)+'-';
 return els.map((el,index)=>{
 const r=el.getBoundingClientRect(),s=getComputedStyle(el);
 const labels=[el.innerText,el.value,el.getAttribute('aria-label'),el.getAttribute('title'),el.getAttribute('alt'),
 ...(el.getAttribute('aria-labelledby')||'').split(/\s+/).map(id=>document.getElementById(id)?.innerText),
 ...Array.from(el.querySelectorAll('img[alt]')).map(i=>i.alt)].filter(Boolean);
 let domestic=false;
 for(let p=el.parentElement,n=0;p&&n<7;p=p.parentElement,n++){
  const t=p.innerText||'';
  const c=Array.from(p.querySelectorAll('a,button,input[type=image],input[type=button],input[type=submit],[role=button]')).filter(x=>
   /報告書閲覧|報告書一覧/.test([x.innerText,x.value,x.getAttribute('alt'),
    ...Array.from(x.querySelectorAll('img[alt]')).map(i=>i.alt)].filter(Boolean).join(' '))).length;
  if(t.length<1800&&t.includes('国内株式')&&t.includes('投資信託')&&c<=1){domestic=true;break;}
 }
 // The DOM may insert a control between discovery and click. A marker refers
 // to this exact element; a saved nth(index) could click a different action.
 const marker=prefix+index;
 el.setAttribute('data-family-trades-nav',marker);
 return {index,marker,labels,domestic,href:el.getAttribute('href')||'',
 visible:!!(r.width&&r.height&&s.visibility!=='hidden'&&s.display!=='none'),
 enabled:!el.disabled&&!el.closest('[inert],[aria-disabled="true"]')};
 });
}'''

def scan(frame):return frame.locator(SELECTOR).evaluate_all(SCAN)
def select_rows(rows,names,domestic=False):
 wanted={norm(n) for n in names}
 return [r for r in rows if r['visible'] and r['enabled'] and
         (not domestic or r['domestic']) and any(norm(v) in wanted for v in r['labels'])]
def control(frame,row):
 # Older fixtures have no marker. Browser scans always provide one.
 if row.get('marker'):return frame.locator('[data-family-trades-nav="'+row['marker']+'"]')
 return frame.locator(SELECTOR).nth(row['index'])
def controls(frame,names,domestic=False):
 return [control(frame,r) for r in select_rows(scan(frame),names,domestic)]

STEPS=[(['報告書閲覧','報告書一覧'],True),
 (['電子交付書面','電子交付書面等','電子ポスト','電子ポストを開く','電子ポストへ進む','電子交付書面一覧'],False),
 (['取引報告書等（電子交付）','取引報告書等(電子交付)','電子交付サービス'],False),
 (['口座管理'],False)]

def _post(context):
 from epost_collector import is_post_url
 found=[p for p in list(context.pages) if not p.is_closed() and is_post_url(p.url)]
 if len(found)>1:raise CredentialError('電子ポストのタブが複数あります。1つだけ残してからやり直してください。')
 return found[0] if found else None

def _trusted_route(frame):
 from auto_login import trusted
 from epost_collector import POST_HOST
 if trusted(frame):return True
 try:
  u=urlparse(frame.url)
  return u.scheme=='https' and u.hostname==POST_HOST
 except ValueError:return False

def _destination(frame,row):
 href=row.get('href','').strip()
 if not href or href.startswith('#') or href.lower().startswith('javascript:'):return None
 try:return urljoin(frame.url,href)
 except ValueError:return None

def _safe_link(frame,row):
 # JavaScript-backed SBI controls are permitted only after exact label and
 # domestic-section matching. Explicit destinations must stay in the workflow.
 href=row.get('href','').strip()
 if not href or href.startswith('#') or href.lower().startswith('javascript:'):return True
 from epost_collector import POST_HOST
 try:
  u=urlparse(_destination(frame,row) or '')
  return u.scheme=='https' and (u.hostname==POST_HOST or u.hostname=='sbisec.co.jp' or (u.hostname or '').endswith('.sbisec.co.jp'))
 except ValueError:return False

def _equivalent(group):
 if len(group)<=1:return group
 # A repeated header/footer anchor to exactly the same destination is safe.
 # Do not guess between separate JavaScript controls or separate report lists.
 destinations=[_destination(f,r) for f,r,_ in group]
 if destinations[0] and len(set(destinations))==1:return group[:1]
 return []

def _pause(page,context,milliseconds):
 live=[p for p in list(context.pages) if not p.is_closed()]
 if not live:raise CredentialError('SBIのブラウザー画面が閉じられました。')
 (page if page in live else live[-1]).wait_for_timeout(milliseconds)

def _hosts(context):
 # Never log a session URL, query, page text, account number or form value.
 result=set()
 for p in list(context.pages):
  if p.is_closed():continue
  try:host=urlparse(p.url).hostname
  except ValueError:continue
  if host:result.add(host)
 return '、'.join(sorted(result)[:6]) or '未確定'

def open_post(page,context,timeout=30,clock=time.monotonic,progress=lambda _:None,cancelled=lambda:False):
 deadline=clock()+timeout;visited=set();counts=(0,0,0,0);transitions=0;max_step=3;last_counts=None
 while clock()<deadline:
  if cancelled():raise CredentialError('中止しました。')
  post=_post(context)
  if post is not None:return post
  groups=[[],[],[],[]]
  for p in list(context.pages):
   if p.is_closed():continue
   for f in frames(p):
    if not _trusted_route(f):continue
    try:
     rows=scan(f)
     for i,(names,domestic) in enumerate(STEPS):
      if i>max_step:continue
      for r in select_rows(rows,names,domestic):
       if not _safe_link(f,r):continue
       # Include frame identity: equal URLs in different frames are not interchangeable.
       sig=(id(f),f.url,i,r['href'],tuple(r['labels']))
       if sig not in visited:groups[i].append((f,r,sig))
    except Exception:continue # Detached frames are rescanned on the next pass.
  counts=tuple(len(g) for g in groups)
  if counts!=last_counts:
   progress("電子ポスト移動候補：閲覧=%d／電子ポスト=%d／電子交付=%d／口座管理=%d"%counts)
   last_counts=counts
  for i,g in enumerate(groups):
   if not g:continue
   g=_equivalent(g)
   if not g:break
   f,r,sig=g[0]
   progress('電子ポスト移動：'+STEPS[i][0][0]+' を開きます。')
   # Report viewing can start a cross-site POST/SSO popup whose navigation is
   # slower than the click timeout. Monitor pages below, rather than making the
   # successful click wait for the remote document's load event.
   visited.add(sig);transitions+=1
   # After 報告書閲覧 an intermediate SBI page may expose 電子ポスト. The old
   # i-1 guard became -1, excluding every control for the rest of the attempt.
   max_step=min(max_step,1 if i==0 else i-1)
   if transitions<=4:deadline=clock()+timeout
   try:control(f,r).click(timeout=7000,no_wait_after=True)
   except Exception:
    progress('リンク操作後の遷移を確認中です。別タブ・SSOの読み込みを待ちます。')
   break
  if transitions>=6:break
  _pause(page,context,300)
 raise CredentialError(('電子ポスト移動：国内株式の報告書閲覧候補=%d、電子ポスト候補=%d、電子交付メニュー候補=%d、口座管理候補=%d。自動移動を完了できませんでした。'%counts)+' 最終表示先：'+_hosts(context)+'。')

def open_post_resilient(page,context,progress=lambda _:None,cancelled=lambda:False,manual_seconds=180,clock=time.monotonic):
 try:return open_post(page,context,progress=progress,cancelled=cancelled,clock=clock)
 except CredentialError as error:
  if cancelled():raise
  progress(str(error))
  progress('Edgeを閉じずに待機します（最大3分）。開いているEdgeで「取引報告書等（電子交付）」から国内株式・投資信託の報告書一覧を開いてください。表示を検出したら自動再開します。')
  try:page.bring_to_front()
  except Exception:pass
  deadline=clock()+manual_seconds
  while clock()<deadline:
   if cancelled():raise CredentialError('中止しました。')
   post=_post(context)
   if post is not None:
    progress('電子ポストを検出しました。名義確認へ進みます。')
    return post
   _pause(page,context,500)
  raise CredentialError('電子ポスト一覧を待機時間内に確認できませんでした。保存済みPDFと登録データは保持しています。')
