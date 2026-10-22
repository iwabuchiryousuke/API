"""Read-only PDF acquisition from the user's authenticated electronic post.

Card-scoped controls, request generations, structural PDF checks and reconciled
counts prevent a neighboring or stale PDF from silently becoming a success.
Ledger registration remains a separate reviewed operation.
"""
import base64
import json
import hashlib
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from login_credentials import CredentialError, load_credentials


BASE = Path(__file__).resolve().parent
POST_HOST = 'plus.e-shishobako.ne.jp'
MAX_PDF_BYTES = 25_000_000


# Discover each physical document card, including notices the importer cannot yet
# parse.  A document title/date is not unique: several reports may share both.
CONTROL_SELECTOR = 'button,[role="button"],a'
DISCOVER = r"""() => {
 const controls=[...document.querySelectorAll('button,[role="button"],a')];
 const visible=el=>{const r=el.getBoundingClientRect();
   const s=typeof getComputedStyle==='function'?getComputedStyle(el):{};
   return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';};
 const clean=s=>(s||'').replace(/\s+/g,' ').trim();
 const titleWords=['外国債券利金・償還金お支払のご案内（兼）支払通知書',
   '外国債券利金・償還金お支払のご案内','株式等利益剰余金配当金のお知らせ',
   '投資信託取引報告書（特定振替等）','投資信託取引報告書',
   '株式等配当金のお知らせ','譲渡益税徴収・還付のお知らせ','汎用累投売買報告書',
   '割当株式等のお知らせ','割当株券のお知らせ','取引残高報告書','株式取引報告書','取引報告書'];
 const cards=new Map();
 for(let index=0;index<controls.length;index++){
  const control=controls[index]; if(!visible(control))continue;
  let card=typeof control.closest==='function'?control.closest('mat-expansion-panel,[data-document-id],[data-delivery-id],[data-report-id]'):null;
  if(!card){
   for(let parent=control.parentElement,depth=0;parent&&depth<8;parent=parent.parentElement,depth++){
    const text=clean(parent.innerText).replace(/有効期限\s*20\d{2}\/\d{1,2}\/\d{1,2}/g,'');
    if(text.length<=1800&&/20\d{2}\/\d{1,2}\/\d{1,2}/.test(text)&&/報告書|通知書|お知らせ|ご案内|明細/.test(text)){card=parent;break;}
   }
  }
  if(!card)continue;
  const header=typeof card.querySelector==='function'?(card.querySelector('mat-expansion-panel-header,[aria-expanded]')||card):card;
  const original=header.innerText||'';
  const text=clean(original).replace(/有効期限\s*20\d{2}\/\d{1,2}\/\d{1,2}/g,'');
  const match=text.match(/20\d{2}\/\d{1,2}\/\d{1,2}/);if(!match||text.length>5000)continue;
  const date=match[0].split('/').map((v,i)=>i?v.padStart(2,'0'):v).join('/');
  const lines=original.split(/\n+/).map(clean).filter(v=>/報告書|通知書|お知らせ|ご案内|明細/.test(v));
  const title=titleWords.find(t=>text.includes(t))||(lines[0]||'電子交付書類').replace(/20\d{2}\/\d{1,2}\/\d{1,2}/g,'').trim().slice(0,160);
  const label=clean([...new Set([control.innerText,control.textContent,control.getAttribute('aria-label'),control.getAttribute('title')].filter(Boolean))].join(' '));
  const href=control.getAttribute('href')||'';
  const isHeader=control===header||['true','false'].includes(control.getAttribute('aria-expanded'));
  const pdf=/PDF|閲覧|ダウンロード/i.test(label)||/\.pdf(?:$|[?#])/i.test(href);
  const star=/^(?:[☆★]|star(?:_border)?|スター(?:付き)?|お気に入り)$/i.test(label)||/スター|お気に入り/.test(label);
  if(star||(!isHeader&&!pdf&&!/^(?:\+|add|expand_more)$|展開|詳細を表示/.test(label)))continue;
  const rank=isHeader?300:(pdf?200:100);
  const old=cards.get(card);if(old&&old.rank>=rank)continue;
  let token=typeof card.getAttribute==='function'?card.getAttribute('data-family-pdf-card'):null;
  if(!token){
   if(typeof window!=='undefined')window.__familyPdfCardSequence=(window.__familyPdfCardSequence||0)+1;
   token=String(typeof window!=='undefined'?window.__familyPdfCardSequence:index);
   if(typeof card.setAttribute==='function')card.setAttribute('data-family-pdf-card',token);
  }
  let stable_id='';
  for(const name of ['data-document-id','data-delivery-id','data-report-id']){
   const value=typeof card.getAttribute==='function'?card.getAttribute(name):null;if(value){stable_id=name+':'+value;break;}
  }
  const fingerprint=text.replace(/[☆★]/g,'').replace(/(?:mark_email_unread|drafts|expand_more|expand_less|star_border|star|add|remove)/g,'').trim();
  const rect=card.getBoundingClientRect?card.getBoundingClientRect():control.getBoundingClientRect();
  const scroller=typeof window!=='undefined'?window.__familyPdfScroll:null;
  const top=(scroller&&scroller!==document.scrollingElement?scroller.scrollTop:((typeof window!=='undefined'&&window.scrollY)||0))+(rect.top||0);
  cards.set(card,{index,date,title,key:'card:'+token+'|'+stable_id+'|'+fingerprint,token,stable_id,fingerprint,rank,top,mode:isHeader?'expand':'pdf',expanded:control.getAttribute('aria-expanded')==='true'});
 }
 const items=[...cards.values()].sort((a,b)=>a.top-b.top||a.index-b.index);
 const seen=new Map();return items.map(item=>{const pair=item.date+'|'+item.title;item.occurrence=seen.get(pair)||0;seen.set(pair,item.occurrence+1);return item;});
}"""

# Only a PDF control within the selected card may be clicked.  Coordinates are
# never evidence that a button belongs to a document.
FIND_VIEW = r"""index => {
 const controls=[...document.querySelectorAll('button,[role="button"],a')];
 const header=controls[index];if(!header)return -1;
 let card=header.closest('[data-family-pdf-card],mat-expansion-panel,[data-document-id],[data-delivery-id],[data-report-id]');
 if(!card)return -1;
 const views=[...card.querySelectorAll('button,[role="button"],a')].filter(el=>{
  const r=el.getBoundingClientRect(),s=getComputedStyle(el);if(!r.width||!r.height||s.visibility==='hidden'||s.display==='none'||el.disabled||el.getAttribute('aria-disabled')==='true')return false;
  if(el===header||el.hasAttribute('aria-expanded'))return false;
  const label=[el.innerText,el.textContent,el.getAttribute('aria-label'),el.getAttribute('title')].filter(Boolean).join(' ').replace(/\s+/g,' ').trim();
  if(/スター|お気に入り|^(?:[☆★]|star(?:_border)?)$/i.test(label))return false;
  return /PDF/i.test(label)||/\.pdf(?:$|[?#])/i.test(el.getAttribute('href')||'')||/^(?:閲覧|ダウンロード|保存)$/.test(label);
 });
 if(views.length!==1)return views.length?-2:-1;
 return controls.indexOf(views[0]);
}"""

SCROLL = r"""({top,step}) => {
 let scroller=window.__familyPdfScroll;
 if(!scroller||!scroller.isConnected){
  const candidates=[document.scrollingElement,...document.querySelectorAll('cdk-virtual-scroll-viewport,[role="feed"],main,section,div')].filter(Boolean);
  scroller=candidates.filter(el=>{
   if(el===document.scrollingElement)return true;
   const style=getComputedStyle(el),r=el.getBoundingClientRect();
   return r.width>200&&r.height>100&&el.scrollHeight>el.clientHeight+10&&/(auto|scroll)/.test(style.overflowY)&&el.querySelector('[data-family-pdf-card],mat-expansion-panel');
  }).sort((a,b)=>(b.scrollHeight-b.clientHeight)-(a.scrollHeight-a.clientHeight))[0]||document.scrollingElement;
  window.__familyPdfScroll=scroller;
 }
 const before=scroller.scrollTop,viewport=scroller===document.scrollingElement?window.innerHeight:scroller.clientHeight;
 scroller.scrollTop=typeof top==='number'?Math.max(0,top):before+Math.max(200,Math.floor(viewport*(step||.8)));
 return {before,after:scroller.scrollTop,height:scroller.scrollHeight,viewport};
}"""


LOGIN_FIELDS = r"""() => {
  const inputs=[...document.querySelectorAll('input')];
  const visible=input => {
    const rect=input.getBoundingClientRect();
    const style=(typeof getComputedStyle==='function') ? getComputedStyle(input) : {display:'block',visibility:'visible'};
    return rect.width>0 && rect.height>0 && style.display!=='none' && style.visibility!=='hidden' && !input.disabled;
  };
  let password=-1, username=-1, userScore=-1;
  for(let i=0;i<inputs.length;i++) {
    const input=inputs[i]; if(!visible(input)) continue;
    const type=(input.type||'text').toLowerCase();
    const context=[input.name,input.id,input.autocomplete,input.placeholder,
      input.getAttribute('aria-label'),input.parentElement && input.parentElement.innerText]
      .filter(Boolean).join(' ').toLowerCase();
    if(type==='password' && password<0) password=i;
    if(['text','tel','email'].includes(type)) {
      let score=0;
      if(/ユーザー|ユーザーネーム|user|login|account|口座/.test(context)) score+=20;
      if(/検索|search|銘柄/.test(context)) score-=30;
      if((input.autocomplete||'').toLowerCase()==='username') score+=50;
      if(score>userScore){userScore=score;username=i;}
    }
  }
  return username>=0 && password>=0 && userScore>=0 ? {username,password} : null;
}"""


def is_post_url(url):
    try:
        parsed = urlparse(url)
        route = parsed.fragment.split('?', 1)[0].rstrip('/')
        return (parsed.scheme == 'https' and parsed.hostname == POST_HOST
                and parsed.username is None and parsed.password is None
                and parsed.port in (None, 443)
                and (route in ('/user-delivery', 'user-delivery')
                     or parsed.path.rstrip('/').endswith('/user-delivery')))
    except (ValueError, AttributeError):
        return False


def wait_for_post_tab(context, seconds=60):
    """The report link opens a new tab, briefly at about:blank."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = [page for page in context.pages
                 if not page.is_closed() and is_post_url(page.url)]
        if len(found) > 1:
            raise RuntimeError('電子ポストのタブが複数あります。1つだけ残してからやり直してください。')
        if found:
            return found[0]
        live = [page for page in context.pages if not page.is_closed()]
        if live:
            live[0].wait_for_timeout(500)
        else:
            time.sleep(.5)
    raise RuntimeError('電子ポストの一覧が開いていません。書類一覧を表示してからやり直してください。')


def login_diagnostics(context):
    counts={'trusted':0,'password':0,'passkey':0}
    for page in context.pages:
        for frame in page.frames:
            try:
                host=(urlparse(frame.url).hostname or '').lower()
                if urlparse(frame.url).scheme!='https' or not (host=='sbisec.co.jp' or host.endswith('.sbisec.co.jp')):continue
                counts['trusted']+=1
                fields=frame.locator('input[type="password"]')
                counts['password']+=sum(fields.nth(i).is_visible() for i in range(fields.count()))
                counts['passkey']+=int('パスキー' in frame.locator('body').inner_text(timeout=1000))
            except Exception:continue
    return f"画面診断：SBIフレーム{counts['trusted']}、表示パスワード欄{counts['password']}、パスキー表記あり{counts['passkey']}。"


def _login_page(context):
    """Return the unique trusted page/frame containing the password form."""
    found=[]
    for page in reversed(context.pages):
        if page.is_closed():continue
        for frame in page.frames:
            try:
                host=(urlparse(frame.url).hostname or '').lower()
                if urlparse(frame.url).scheme!='https' or not (host=='sbisec.co.jp' or host.endswith('.sbisec.co.jp')):continue
                if frame.evaluate(LOGIN_FIELDS):found.append(frame)
            except Exception:continue
    if len(found)>1:raise CredentialError('ログイン入力欄が複数あります。別のログインタブを閉じて再実行してください。')
    return found[0] if found else None


def _open_login(page, context):
    """Open the ordinary SBI login form when the home page exposes a login link."""
    if _login_page(context):
        return
    try:
        controls = page.locator('a,button,[role="button"]')
        candidates = []
        for index in range(min(controls.count(), 300)):
            item = controls.nth(index)
            try:
                if not item.is_visible():
                    continue
                label = re.sub(r'\s+', '', item.inner_text(timeout=200))
                if label == 'ログイン':
                    box = item.bounding_box() or {}
                    candidates.append((box.get('y', 99999), -box.get('x', 0), index))
            except Exception:
                continue
        if candidates:
            controls.nth(sorted(candidates)[0][2]).click(timeout=5000)
            page.wait_for_timeout(1500)
            return True
    except Exception:
        pass


def autofill_login(page, context, account, automatic=False):
    """Fill, but never submit, credentials protected by Windows DPAPI."""
    try:
        credentials = load_credentials(BASE / 'data', account)
    except CredentialError as error:
        print('ログイン情報を自動入力できません:', error)
        return False
    if credentials is None:
        print('ログイン情報は未設定です。今回は手動で入力してください。')
        return False
    deadline = time.monotonic() + 45
    opened=False
    target = None
    fields = None
    while time.monotonic() < deadline:
        target = _login_page(context)
        if target is not None:
            fields = target.evaluate(LOGIN_FIELDS)
            if fields:
                break
        if not opened and page.locator('a,button,[role="button"]').count():
            opened=bool(_open_login(page,context))
        page.wait_for_timeout(400)
    if target is None or not fields:
        print('ログイン欄を見つけられませんでした。画面で手動入力してください。')
        return False
    from urllib.parse import urlparse
    host=(urlparse(target.url).hostname or '').lower()
    if host!='sbisec.co.jp' and not host.endswith('.sbisec.co.jp'):
        raise CredentialError('SBIのログイン画面であることを確認できません。')
    inputs = target.locator('input')
    inputs.nth(fields['username']).fill(credentials[0])
    inputs.nth(fields['password']).fill(credentials[1])
    print('ユーザーネームとログインパスワードを入力しました。')
    if not automatic:print('内容を確認してログインを押し、メール認証コードはご自身で入力してください。')
    return True


def safe_part(text):
    return re.sub(r'[^0-9A-Za-zぁ-んァ-ヶ一-龯ー_-]+', '_', text).strip('_')[:60] or '書類'


def validate_pdf(raw):
    """Reject HTML, truncated downloads and unreadable PDF containers."""
    import io
    from pypdf import PdfReader
    if not isinstance(raw, bytes) or not raw.startswith(b'%PDF-') or len(raw) > MAX_PDF_BYTES:
        raise ValueError('PDFではないか、25MBを超えるため保存しませんでした。')
    if b'%%EOF' not in raw[-4096:]:
        raise ValueError('PDFの終端がなく、取得が完了していません。')
    try:
        reader = PdfReader(io.BytesIO(raw), strict=False)
        if reader.is_encrypted and not reader.decrypt(''):
            raise ValueError('パスワード付きPDFは自動確認できません。')
        if len(reader.pages) < 1:
            raise ValueError('ページがありません。')
    except Exception as error:
        raise ValueError('PDF本体を読み取れません。再取得が必要です。') from error


def _document_kind(text):
    # Specific form identifiers/titles, never incidental words such as 外国債券
    # or 配当金 in a holdings table or disclosure footer.
    text = re.sub(r'\s+', '', text)
    if any(marker in text for marker in ('STWHZB', 'FR00011M01', '取引残高報告書')):
        return 'balance'
    for kind, words in [
        ('dividend', ('株式等利益剰余金配当金のお知らせ', '株式等配当金のお知らせ')),
        ('tax', ('譲渡益税徴収・還付のお知らせ',)),
        ('allocation', ('割当株券のお知らせ', '割当株式等のお知らせ')),
        ('bond', ('外国債券利金・償還金お支払のご案内',)),
        ('trade', ('取引報告書', '売買報告書')),
    ]:
        if any(word in text for word in words):
            return kind
    return None


def _verify_document(raw, item):
    """A clearly conflicting document title must not be associated with a card.

    Delivery date and statement-period date differ, so date guessing is unsafe.
    Unextractable/unknown titles are recorded as unconfirmed, never invented.
    """
    import io
    import pdfplumber
    validate_pdf(raw)
    wanted = _document_kind(item['title'])
    try:
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            text = (pdf.pages[0].extract_text() or '')[:1200]
    except Exception:
        return '本文の書類名は未確認'
    found = _document_kind(text)
    if wanted and found and wanted != found:
        raise ValueError('一覧の書類名とPDF本文の種類が一致しません。別書類として保存しません。')
    return '本文の書類種類も照合' if wanted and found == wanted else '本文の書類名は未確認'


def save_pdf(root, account, document_date, title, raw):
    validate_pdf(raw)
    if account not in 'ABCDE' or len(account) != 1:
        raise ValueError('口座A～Eを指定してください。')
    folder = Path(root) / account
    folder.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(raw).hexdigest()
    for previous in folder.glob(f'*_{digest[:16]}.pdf'):
        if hashlib.sha256(previous.read_bytes()).hexdigest() == digest:
            return previous, False
    filename = f'{safe_part(document_date)}_{safe_part(title)}_{digest[:16]}.pdf'
    destination = folder / filename
    if destination.exists():
        if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
            raise ValueError('同名のファイルの内容が異なります。手動で確認してください。')
        return destination, False
    import tempfile
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=folder, prefix='.pdf_', suffix='.part', delete=False) as output:
            temporary = Path(output.name)
            output.write(raw)
        temporary.replace(destination)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    manifest = folder / '保存一覧.jsonl'
    with manifest.open('a', encoding='utf-8') as output:
        output.write(json.dumps(dict(date=document_date, title=title, filename=filename, sha256=digest), ensure_ascii=False) + '\n')
    return destination, True


def _trusted_pdf_url(url):
    try:
        parsed = urlparse(url.removeprefix('blob:'))
        host = (parsed.hostname or '').lower()
        return parsed.scheme == 'https' and parsed.username is None and parsed.password is None and parsed.port in (None, 443) and (host == POST_HOST or host == 'sbisec.co.jp' or host.endswith('.sbisec.co.jp'))
    except (ValueError, AttributeError):
        return False


def take_pdf(caught_responses, caught_downloads, pages):
    """Read only artifacts from the current PDF action and refuse ambiguity."""
    artifacts = {}
    def accept(raw):
        if raw.startswith(b'%PDF-') and len(raw) <= MAX_PDF_BYTES:
            artifacts[hashlib.sha256(raw).hexdigest()] = raw
    for download in caught_downloads:
        try:
            if not _trusted_pdf_url(download.url):
                continue
            path = download.path()
            if path and Path(path).stat().st_size <= MAX_PDF_BYTES:
                accept(Path(path).read_bytes())
        except Exception:
            pass
    for response in caught_responses:
        try:
            if not _trusted_pdf_url(response.url):
                continue
            headers = response.headers
            mime = headers.get('content-type', '').lower()
            disposition = headers.get('content-disposition', '').lower()
            if 'pdf' in mime or '.pdf' in disposition or ('octet-stream' in mime and '.pdf' in response.url.lower()):
                declared = headers.get('content-length', '')
                if declared.isdigit() and int(declared) > MAX_PDF_BYTES:
                    continue
                accept(response.body())
        except Exception:
            pass
    for page in pages:
        try:
            if page.is_closed() or not page.url.startswith('blob:') or not _trusted_pdf_url(page.url):
                continue
            data = page.evaluate("""async () => {
              const blob=await (await fetch(location.href)).blob();
              if(blob.size>25000000) return null;
              return await new Promise((resolve,reject) => {
                const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);
                reader.onerror=reject;reader.readAsDataURL(blob);
              });
            }""")
            if data:
                accept(base64.b64decode(data, validate=True))
        except Exception:
            pass
    if len(artifacts) > 1:
        raise ValueError('1つの書類操作から異なるPDFを複数検出しました。書類との対応を確認できません。')
    return next(iter(artifacts.values()), None)


def _check_cancelled(cancelled):
    if cancelled():
        raise CredentialError('中止しました。保存済みPDFは保持しています。')


def _dismiss_menu(page):
    """Dismiss the account menu observed in diagnostics, never force clicks."""
    overlays = page.locator('.cdk-overlay-transparent-backdrop.cdk-overlay-backdrop-showing')
    if overlays.count():
        page.keyboard.press('Escape')
        # A real dialog is deliberately not auto-accepted or clicked through.
        try:
            overlays.first.wait_for(state='hidden', timeout=2000)
        except Exception as error:
            raise RuntimeError('メニューやダイアログが一覧を覆っています。画面を確認してください。') from error


def discover_all(page, max_scrolls=100, cancelled=lambda: False):
    """Scan the lazy list, retaining identical-looking but distinct cards."""
    items = {}
    stable_bottom = 0
    last = None
    _dismiss_menu(page)
    page.evaluate(SCROLL, {'top': 0})
    page.wait_for_timeout(150)
    for _ in range(max_scrolls):
        _check_cancelled(cancelled)
        for item in page.evaluate(DISCOVER):
            previous = items.get(item['key'])
            if previous is None or item['top'] < previous['top']:
                items[item['key']] = item
        state = page.evaluate(SCROLL, {'step': .8})
        marker = (state['after'], state['height'], len(items))
        at_bottom = state['after'] + state['viewport'] >= state['height'] - 4
        stable_bottom = stable_bottom + 1 if at_bottom and marker == last else 0
        last = marker
        page.wait_for_timeout(180)
        if stable_bottom >= 3:
            break
    if stable_bottom < 3:
        raise RuntimeError('一覧の末尾を確認できません。取得完了とは扱いません。期間を絞って再実行してください。')
    page.evaluate(SCROLL, {'top': 0})
    page.wait_for_timeout(100)
    return sorted(items.values(), key=lambda item: (item['top'], item['date'], item['title']))


def _resolve_index(page, item):
    page.evaluate(SCROLL, {'top': max(0, item.get('top', 0) - 160)})
    page.wait_for_timeout(100)
    current = page.evaluate(DISCOVER)
    exact = [value for value in current if value['key'] == item['key']
             and (not item.get('stable_id') or value.get('stable_id') == item['stable_id'])]
    if len(exact) == 1:
        return exact[0]['index']
    # A virtual list may recreate a DOM node. Only an unambiguous full metadata
    # match is acceptable; date/title/occurrence guesses previously saved wrong PDFs.
    same = [value for value in current if item.get('fingerprint') and value.get('fingerprint') == item['fingerprint']]
    if item.get('stable_id'):
        same = [value for value in same if value.get('stable_id') == item['stable_id']]
    return same[0]['index'] if len(same) == 1 else -1


class _CapturedPDFResponse:
    def __init__(self, url, headers, raw):
        self.url, self.headers, self.raw = url, headers, raw
    def body(self):
        return self.raw


class _ResponseCapture(list):
    """Ignore responses whose request began before the current PDF action."""
    def __init__(self, owner=None, context=None):
        super().__init__()
        self.owner = owner
        self.context = context
        self.generation = 0
        self.active = False
        self.requests = {}
        self.request_urls = set()
        self.expected_urls = set()
        self.seen_urls = {}
        self.ambiguous_urls = set()
        self.document_key = None
        self.current_requests = set()
        self.finished_requests = set()
        self.replayed = set()
    def begin(self, expected_url=None, document_key=None):
        self.generation += 1
        self.active = True
        self.document_key = document_key if document_key is not None else self.generation
        self.request_urls.clear()
        self.current_requests.clear()
        self.finished_requests.clear()
        self.replayed.clear()
        self.expected_urls = {expected_url} if isinstance(expected_url, str) and _trusted_pdf_url(expected_url) else set()
        for url in self.expected_urls:
            self._record_url(url)
        self.clear()
    def _record_url(self, url):
        if url in self.seen_urls and self.seen_urls[url] != self.document_key:
            self.ambiguous_urls.add(url)
        self.seen_urls[url] = self.document_key
    def finish(self):
        self.active = False
        self.requests.clear()
        self.request_urls.clear()
        self.expected_urls.clear()
    def allows_download(self, download):
        return self.active and _trusted_pdf_url(download.url) and download.url not in self.ambiguous_urls and download.url in (self.request_urls | self.expected_urls)
    def owns(self, page):
        if self.owner is None:
            return True
        for _ in range(3):
            if page is self.owner:
                return True
            try:
                page = page.opener()
            except Exception:
                return False
            if page is None:
                return False
        return False
    def request(self, request):
        if self.active:
            try:
                if self.owner is not None and not self.owns(request.frame.page):
                    return
            except Exception:
                return
            self.requests[request] = self.generation
            self.current_requests.add(request)
            self.request_urls.add(request.url)
            self._record_url(request.url)
    def completed(self, request):
        if self.active and request in self.current_requests:
            self.finished_requests.add(request)
    def response(self, response):
        generation = self.requests.pop(response.request, None)
        if self.active and generation == self.generation and _trusted_pdf_url(response.url):
            self.append(response)
    def replay_missing(self, cancelled=lambda: False):
        # Chromium sometimes discards a response body after the web app creates
        # a downloadable blob. Re-fetch only the EXACT completed PDF request,
        # using the same context authentication; never replay a generic POST.
        if self.context is None:
            return
        for response in list(self):
            if isinstance(response, _CapturedPDFResponse) or response in self.replayed:
                continue
            try:
                request = response.request
                headers = response.headers
                mime = headers.get('content-type', '').lower()
                disposition = headers.get('content-disposition', '').lower()
                if (request not in self.finished_requests or response.status != 200
                        or request.method not in ('GET', 'POST')
                        or not _trusted_pdf_url(request.url)
                        or not ('pdf' in mime or '.pdf' in disposition)):
                    continue
                self.replayed.add(response)
                _check_cancelled(cancelled)
                downloaded = self.context.request.fetch(request, timeout=7000, max_redirects=0, fail_on_status_code=False)
                try:
                    h = downloaded.headers
                    size = h.get('content-length', '')
                    if (downloaded.status != 200 or not _trusted_pdf_url(downloaded.url)
                            or not ('pdf' in h.get('content-type', '').lower() or '.pdf' in h.get('content-disposition', '').lower())
                            or (size.isdigit() and int(size) > MAX_PDF_BYTES)):
                        continue
                    raw = downloaded.body()
                    if raw.startswith(b'%PDF-') and len(raw) <= MAX_PDF_BYTES:
                        self.append(_CapturedPDFResponse(downloaded.url, h, raw))
                finally:
                    downloaded.dispose()
            except CredentialError:
                raise
            except Exception:
                # Unknown/expired/method-specific URLs remain explicit failures.
                continue


def _wait_pdf(page, responses, downloads, popups, starts, seconds=12, cancelled=lambda: False):
    deadline = time.monotonic() + seconds
    first = None
    received_at = None
    while time.monotonic() < deadline:
        _check_cancelled(cancelled)
        current_popups = popups[starts[2]:]
        if isinstance(responses, _ResponseCapture):
            # A blob created later by the previous card cannot be attributed by
            # timing. Only an explicit current-card blob href is acceptable.
            current_popups = [p for p in current_popups if p.url in responses.expected_urls]
        raw = take_pdf(responses[starts[0]:], downloads[starts[1]:], current_popups)
        if raw is None and isinstance(responses, _ResponseCapture):
            responses.replay_missing(cancelled=cancelled)
            raw = take_pdf(responses[starts[0]:], downloads[starts[1]:], current_popups)
        if raw is not None:
            if first is None:
                first, received_at = raw, time.monotonic()
            elif raw != first:
                raise ValueError('同じ書類操作でPDF本体が変わりました。対応関係を確認できません。')
            # Drain response/download events from this action before starting the
            # next card. This also catches response + delayed download ambiguity.
            if time.monotonic() - received_at >= .24:
                return first
        page.wait_for_timeout(80)
    return first


def _retrieve(page, context, item, responses, downloads, popups, cancelled=lambda: False):
    _check_cancelled(cancelled)
    _dismiss_menu(page)
    index = _resolve_index(page, item)
    if index < 0:
        raise ValueError('一覧の書類を一意に再発見できません。日付と書類名だけで推測しません。')
    controls = page.locator(CONTROL_SELECTOR)
    control = controls.nth(index)
    control.scroll_into_view_if_needed(timeout=4000)
    # Expanding a card is not a PDF action: don't capture unrelated page traffic.
    if item.get('mode', 'expand') == 'expand':
        if control.get_attribute('aria-expanded') != 'true':
            control.click(timeout=5000)
        deadline = time.monotonic() + 4
        view = -1
        while time.monotonic() < deadline:
            _check_cancelled(cancelled)
            index = _resolve_index(page, item)
            view = page.evaluate(FIND_VIEW, index) if index >= 0 else -1
            if view == -2:
                raise ValueError('同じ書類内にPDF操作候補が複数あります。自動で選択しません。')
            if view >= 0:
                break
            page.wait_for_timeout(100)
        if view < 0:
            raise ValueError('選択した書類の中にPDF閲覧ボタンが見つかりません。')
        control = page.locator(CONTROL_SELECTOR).nth(view)
    if isinstance(responses, _ResponseCapture):
        responses.begin(control.get_attribute('href'), document_key=item['key'])
    starts = (len(responses), len(downloads), len(popups))
    try:
        control.click(timeout=5000)
        raw = _wait_pdf(page, responses, downloads, popups, starts, cancelled=cancelled)
        if raw is None:
            raise ValueError('選択した書類のPDF本体を受信できませんでした。')
        return raw, starts[2]
    finally:
        if isinstance(responses, _ResponseCapture):
            responses.finish()


def _cache_identity(account, item):
    if not item.get('stable_id') or not item.get('fingerprint'):
        return None
    payload = [account, item['stable_id'], item['fingerprint']]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


class _DocumentCache:
    """Only new stable source IDs; never trust historical mislabeled audit files."""
    def __init__(self, folder, account):
        self.folder = Path(folder) / account
        self.path = self.folder / '取得キャッシュ_v1.json'
        self.account = account
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            self.entries = data.get('entries', {}) if data.get('version') == 1 else {}
            if not isinstance(self.entries, dict):
                self.entries = {}
        except (OSError, ValueError, AttributeError):
            self.entries = {}
        self.original = dict(self.entries)
        self.invalidated = set()
    def get(self, item):
        key = _cache_identity(self.account, item)
        entry = self.entries.get(key) if key else None
        if not isinstance(entry, dict):
            return None
        filename = entry.get('file', '')
        if not filename or Path(filename).name != filename:
            return None
        path = self.folder / filename
        try:
            if path.resolve().parent != self.folder.resolve():
                return None
            if path.stat().st_size > MAX_PDF_BYTES:
                return None
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != entry.get('sha256'):
                return None
            validate_pdf(raw)
            return path, raw
        except (OSError, ValueError):
            return None
    def put(self, item, path, raw):
        key = _cache_identity(self.account, item)
        if not key:
            return
        self.entries[key] = {'file': path.name, 'sha256': hashlib.sha256(raw).hexdigest()}
    def remove(self, item):
        key = _cache_identity(self.account, item)
        if key:
            self.entries.pop(key, None)
            self.invalidated.add(key)
    def flush(self, commit=False):
        # A later collision can invalidate an earlier association. New cache
        # entries become durable only after the whole selected list reconciles.
        if not commit:
            self.entries = {k: v for k, v in self.original.items() if k not in self.invalidated}
        if self.entries == self.original:
            return
        self.folder.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'version': 1, 'entries': self.entries}, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.path)


def _next_page(page):
    candidates = []
    names = re.compile(r'^(?:次へ|次のページ|次ページ|Next|Next page|>|›)$', re.I)
    for role in ('link', 'button'):
        loc = page.get_by_role(role, name=names)
        for i in range(loc.count()):
            control = loc.nth(i)
            if not control.is_visible() or not control.is_enabled():
                continue
            label = control.get_attribute('aria-label') or control.inner_text()
            pagination = control.evaluate("el=>!!el.closest('mat-paginator,[class*=paginator],[class*=pagination],nav[aria-label*=ページ],nav[aria-label*=Pagination]')")
            if not pagination and not re.search(r'ページ|Next page', label, re.I):
                raise RuntimeError('次へ操作を検出しましたがページ送りとは確認できません。画面を確認してください。')
            candidates.append(control)
    if len(candidates) > 1:
        raise RuntimeError('次ページ操作が複数あります。安全にページを特定できません。')
    return candidates[0] if candidates else None


def _page_signature(items):
    return tuple((item.get('stable_id', ''), item['date'], item['title'], item.get('fingerprint', item['key'])) for item in items)


def collect(page, context, account, folder, limit=500, progress=print, cancelled=lambda: False):
    _check_cancelled(cancelled)
    candidates = discover_all(page, cancelled=cancelled)
    counts = re.findall(r'全\s*([\d,]+)\s*件', page.locator('body').inner_text())
    expected = int(counts[0].replace(',', '')) if counts and len(set(counts)) == 1 else None
    from collection_audit import Audit
    audit = Audit(folder, account, candidates, expected, defer=True)
    audit.data.update(complete=False, download_failed=0, unresolved=0, pages=0)
    audit.data['note'] += ' 取得後にPDF構造を検証し、判読できる本文の書類種類を照合します。交付日とPDFの作成日・基準日は同一とは限りません。'
    progress('取得結果一覧：' + str(audit.folder / (audit.stem + '.html')))
    responses, downloads, popups = _ResponseCapture(page, context), [], []
    registrations = []
    def listen(emitter, event, callback):
        emitter.on(event, callback)
        registrations.append((emitter, event, callback))
    def download(download):
        if responses.allows_download(download):
            downloads.append(download)
    def register(new_page):
        if responses.active and responses.owns(new_page):
            popups.append(new_page)
            listen(new_page, 'download', download)
    listen(context, 'request', responses.request)
    listen(context, 'response', responses.response)
    listen(context, 'requestfinished', responses.completed)
    listen(context, 'page', register)
    listen(page, 'download', download)
    cache = _DocumentCache(folder, account)
    saved = duplicates = 0
    failures = []
    failed_indices = set()
    signatures = set()
    digests = {}
    all_items = []
    try:
        for page_number in range(1, 101):
            _check_cancelled(cancelled)
            signature = _page_signature(candidates)
            if signature in signatures:
                raise RuntimeError('次ページに移動しても同じ一覧です。重複した画面を全件取得とは扱いません。')
            signatures.add(signature)
            if not candidates:
                if expected == 0 and page_number == 1:
                    audit.data.update(complete=True, discovered=0, pages=1)
                    progress('画面件数0件と空の一覧を照合しました。')
                    return 0, 0, 0
                raise RuntimeError('対象書類が0件です。画面・期間の確認が必要です。')
            if len(all_items) + len(candidates) > limit:
                raise RuntimeError('取得上限を超えています。期間を絞って再実行してください。')
            if page_number > 1:
                audit.data['documents'].extend(dict(date=i['date'], title=i['title'], status='未処理', file='', star='操作なし', error='') for i in candidates)
            offset = len(all_items)
            all_items.extend(candidates)
            audit.data.update(discovered=len(all_items), pages=page_number)
            progress(f'{page_number}ページ目：書類 {len(candidates)} 件（累計 {len(all_items)} 件）を検出。')
            stable_counts = {}
            for item in candidates:
                stable_id = item.get('stable_id')
                if stable_id:
                    stable_counts[stable_id] = stable_counts.get(stable_id, 0) + 1
            for local_number, item in enumerate(candidates):
                _check_cancelled(cancelled)
                item_number = offset + local_number
                if stable_counts.get(item.get('stable_id')) != 1:
                    # A repeated machine ID is not a safe cross-run cache key.
                    item = dict(item, stable_id='')
                error = None
                for attempt in range(2):
                    popup_start = len(popups)
                    try:
                        hit = cache.get(item)
                        if hit:
                            path, raw = hit
                            created = False
                            evidence = '安定した書類ID・メタデータ・保存済みPDFハッシュを照合'
                        else:
                            raw, _ = _retrieve(page, context, item, responses, downloads, popups, cancelled=cancelled)
                            evidence = _verify_document(raw, item)
                            path = None
                        digest = hashlib.sha256(raw).hexdigest()
                        identity = item['key']
                        if digest in digests and digests[digest]['identity'] != identity:
                            previous = digests[digest]
                            cache.remove(previous['item'])
                            cache.remove(item)
                            if previous['number'] not in failed_indices:
                                failed_indices.add(previous['number'])
                                failures.append((previous['item'], '別の一覧書類と同一PDFのため対応関係を未確認に戻しました。'))
                                audit.update(previous['number'], status='要確認', star='操作なし', error='別の一覧書類でも同一PDFを受信しました。両方の対応関係を確認できません。保存した原本は保持しています。')
                            raise ValueError('別の一覧書類と同一のPDFを受信しました。取り違えの可能性があるため保存・完了扱いにしません。')
                        digests[digest] = {'identity': identity, 'item': item, 'number': item_number}
                        if path is None:
                            path, created = save_pdf(folder, account, item['date'], item['title'], raw)
                            cache.put(item, path, raw)
                        saved += int(created)
                        duplicates += int(not created)
                        audit.update(item_number, status='新規保存' if created else '保存済み', file=path.name, star='操作なし', error=evidence + '。解析・台帳登録は取り込み時に実施')
                        progress(f'{item["date"]} {item["title"]}：PDF保存確認')
                        error = None
                        break
                    except CredentialError:
                        raise
                    except Exception as caught:
                        error = caught if type(caught) in (ValueError, RuntimeError) else RuntimeError('ブラウザー操作またはファイル保存を完了できませんでした。画面と取得結果を確認してください。')
                        if attempt == 0:
                            progress(f'{item["date"]} {item["title"]}：再取得します。')
                            page.wait_for_timeout(250)
                    finally:
                        for popup in popups[popup_start:]:
                            if not popup.is_closed():
                                try:
                                    popup.close()
                                except Exception:
                                    pass
                if error is not None:
                    failed_indices.add(item_number)
                    failures.append((item, str(error)))
                    audit.update(item_number, status='取得失敗', error=str(error), star='操作なし')
                    progress(f'{item["date"]} {item["title"]}：取得失敗：{error}')
                    if len(failures) <= 3:
                        try:
                            page.screenshot(path=str(audit.folder / (audit.stem + f'_失敗{item_number + 1}.png')), full_page=False)
                        except Exception:
                            pass
            _dismiss_menu(page)
            next_control = _next_page(page)
            if next_control is None:
                break
            previous = _page_signature(page.evaluate(DISCOVER))
            next_control.click(timeout=5000)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                _check_cancelled(cancelled)
                current = page.evaluate(DISCOVER)
                if current and _page_signature(current) != previous:
                    break
                page.wait_for_timeout(150)
            else:
                raise RuntimeError('次ページの読み込みを確認できません。取得完了とは扱いません。')
            candidates = discover_all(page, cancelled=cancelled)
        else:
            raise RuntimeError('ページ数が上限を超えています。期間を絞って再実行してください。')
        unresolved = (max(1, abs(expected - len(all_items))) if expected is not None and expected != len(all_items) else int(expected is None))
        audit.data.update(download_failed=len(failures), unresolved=unresolved, complete=not failures and not unresolved)
        if unresolved:
            progress(f'件数照合未完了：画面件数 {expected if expected is not None else "未確認"}、検出件数 {len(all_items)}。全件取得成功とは扱いません。')
        return saved, duplicates, len(failures) + unresolved
    finally:
        responses.finish()
        try:
            cache.flush(commit=audit.data.get('complete', False))
        except OSError:
            audit.data['cache_warning'] = '高速化キャッシュを保存できません。次回もPDFを照合して取得します。'
            progress(audit.data['cache_warning'])
        for emitter, event, callback in reversed(registrations):
            try:
                emitter.remove_listener(event, callback)
            except Exception:
                pass
        audit.flush()

def run(account):
    if account not in 'ABCDE' or len(account) != 1:
        raise ValueError('口座はA～Eです。')
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise RuntimeError('先に setup_epost_windows.bat をダブルクリックして準備してください。') from error
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=False)
        try:
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            page.goto('https://www.sbisec.co.jp/', wait_until='domcontentloaded')
            autofill_login(page, context, account)
            print('開いたEdgeでログインを完了し、対象口座の電子ポスト一覧を開いてください。')
            print('メールで届く認証コードはご自身で入力してください。')
            print('口座を確認し、必要なら交付日や書類の種類を絞り込んでください。')
            input('電子ポスト一覧が表示されたら Enter：')
            post = wait_for_post_tab(context)
            print(f'保存先：口座{account} / {BASE / "data" / "電子ポストPDF" / account}')
            print('画面の書類がこの口座のものであることを確認してください。')
            if input('取得を始めますか？ 「はい」と入力：').strip() != 'はい':
                return
            saved, duplicates, failed = collect(
                post, context, account, BASE / 'data' / '電子ポストPDF')
            print(f'完了：新規 {saved}件、保存済み {duplicates}件、取得できなかった書類 {failed}件。')
            print('この操作では売買記録に登録されません。アプリでPDFを確認してから取り込んでください。')
        finally:
            browser.close()


if __name__ == '__main__':
    try:
        run(sys.argv[1] if len(sys.argv) > 1 else '')
    except Exception as error:
        print(f'停止：{error}')
    input('確認したら Enter で閉じます：')
