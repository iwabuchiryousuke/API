"""Read-only observed SBI navigation. Verify owner at electronic post before saving."""
import re,time,unicodedata,json
from datetime import datetime,timezone,timedelta
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError
from ledger import LedgerError
from login_credentials import CredentialError

class ObservationConflict(LedgerError):
    """Two explicitly labelled displayed balances contradict each other."""
    pass

def _jst_now():
    # Windows Python installations may have no IANA timezone files. Japan has
    # no DST; a fixed UTC+09:00 fallback preserves date boundaries accurately.
    try:zone=ZoneInfo('Asia/Tokyo')
    except ZoneInfoNotFoundError:zone=timezone(timedelta(hours=9),'JST')
    return datetime.now(zone)


def norm(value):return re.sub(r'\s+','',unicodedata.normalize('NFKC',str(value))).rstrip('>›»❯→▶↗')
def frames(page):return list(page.frames) if hasattr(page,'frames') else [page]
def navigate(page,names,timeout=15,cancelled=lambda:False):
    from auto_login import trusted
    from sbi_navigation import controls
    end=time.monotonic()+timeout
    count=0
    while True:
        _check_cancelled(cancelled)
        pages=list(page.context.pages) if hasattr(page,'context') else [page]
        # Prefer the most recently opened SBI tab; e-post has a separate domain.
        for p in reversed(pages):
            if p.is_closed():continue
            choices=[]
            for f in frames(p):
                if not trusted(f):continue
                try:choices.extend(controls(f,names))
                except Exception:continue
            count=len(choices)
            if len(choices)>1:
                hrefs=[x.get_attribute('href') for x in choices]
                if hrefs[0] and not hrefs[0].lower().startswith('javascript:') and len(set(hrefs))==1:choices=choices[:1]
            if len(choices)==1:
                before={id(tab) for tab in pages}
                choices[0].click(timeout=10000);p.wait_for_timeout(200)
                # My資産 can open a separate SBI tab. Keep the verified context
                # and read the new trusted page instead of the old menu.
                current=list(p.context.pages) if hasattr(p,'context') else [p]
                opened=[tab for tab in current if id(tab) not in before and not tab.is_closed() and trusted(tab)]
                return opened[-1] if opened else p
        if time.monotonic()>=end:raise CredentialError('画面移動：'+names[0]+f' の表示候補={count}。')
        page.wait_for_timeout(300)

def landing(context,timeout=40,cancelled=lambda:False):
    from auto_login import trusted
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        _check_cancelled(cancelled)
        for p in context.pages:
            for f in frames(p):
                if not trusted(f):continue
                try:text=f.locator('body').inner_text(timeout=1500)
                except Exception:continue
                # This allows read-only navigation, not collection or owner approval.
                if 'ログアウト' in text and ('口座管理' in text or '取引報告書等' in text):return p
        time.sleep(.3)
    raise CredentialError('認証後のホームを確認できません。認証エラー・追加確認画面を確認してください。')

def verify_post_owner(page,owner,timeout=30,progress=lambda message:None,clock=time.monotonic,cancelled=lambda:False):
    from post_identity import verify
    return verify(page,owner,timeout=timeout,progress=progress,clock=clock,cancelled=cancelled)

def snapshot_dom(page):
    from auto_login import trusted
    texts=[];tables=[]
    for frame in frames(page):
        if not trusted(frame):continue
        try:
            texts.append(frame.locator('body').inner_text(timeout=5000))
            tables+=frame.locator('table').evaluate_all("els=>els.filter(e=>e.getClientRects().length).map(t=>Array.from(t.rows).map(r=>Array.from(r.cells).map(c=>c.innerText)))")
            # Accessible grids used by current React pages are not HTML tables.
            grids=frame.locator('[role="table"],[role="grid"]').evaluate_all("els=>els.filter(e=>e.getClientRects().length).map(t=>Array.from(t.querySelectorAll('[role=row]')).filter(r=>r.getClientRects().length).map(r=>Array.from(r.querySelectorAll('[role=columnheader],[role=rowheader],[role=cell],[role=gridcell]')).map(c=>c.innerText)))")
            tables+=grids
        except Exception:continue # A detached frame is retried by the bounded caller.
    if not texts:raise LedgerError('SBI画面の本文を読み取れません。')
    return '\n'.join(texts),tables

def yen(text):
    from asset_snapshots import money
    return money(text,allow_bare=True)

def _assign(out,key,value,message):
    if value is None:return
    if key in out and out[key]!=value:raise ObservationConflict(message)
    out[key]=value

def _summary_rows(text,tables):
    """Adjacent labelled lines plus real two-cell table rows; no buying power."""
    lines=[norm(x) for x in text.splitlines() if norm(x)]
    rows=[(line,lines[i+1] if i+1<len(lines) else '') for i,line in enumerate(lines)]
    for table in tables:
        rows.extend((norm(row[0]),norm(row[1])) for row in table if len(row)==2)
    return rows

def parse_summary(text,tables=()):
    out={}
    labels={'現金残高等(合計)':'cash_total','現金残高等合計':'cash_total',
            'SBI証券口座分':'broker_cash','スィープ専用銀行口座':'bank','スイープ専用銀行口座':'bank'}
    for line,next_line in _summary_rows(text,tables):
        for label,key in labels.items():
            raw=next_line if line==label else line[len(label):] if line.startswith(label) else ''
            _assign(out,key,yen(raw),'口座サマリーの金額が重複しています。')
        # A complete label carries its own year, so a current limit can never
        # accidentally populate an earlier year's NISA allowance.
        m=re.fullmatch(r'投資可能枠\((\d{4})年\)[−ー―—-]*NISA\((成長投資枠|つみたて投資枠)\)(.*)',line)
        if not m:continue
        raw=m[3] or next_line
        value=yen(raw)
        if value is None or not value.is_integer():continue
        key='growth_remaining' if m[2]=='成長投資枠' else 'tsumitate_remaining'
        _assign(out,key,{'year':int(m[1]),'remaining':int(value)},'NISA枠の表示が重複しています。')
    # Responsive NISA views may put the investment frameworks in headers or
    # rows. Read only the column explicitly labelled remaining/investable JPY;
    # an annual maximum or used amount is not the remaining allowance.
    body=norm(text)
    years=set(int(value) for value in re.findall(r'(?:NISA)?投資可能枠\((\d{4})年\)',body))
    if len(years)==1:
        year=next(iter(years));frameworks={'成長投資枠':'growth_remaining','NISA(成長投資枠)':'growth_remaining','つみたて投資枠':'tsumitate_remaining','NISA(つみたて投資枠)':'tsumitate_remaining'}
        remaining_headers={'投資可能額(円)','投資可能枠(円)','残り投資可能枠(円)','投資可能残額(円)'}
        for table in tables:
            columns={};amount_index=None
            for row in table:
                cells=[norm(cell) for cell in row]
                header={index:frameworks[cell] for index,cell in enumerate(cells) if cell in frameworks}
                indexes=[index for index,cell in enumerate(cells) if cell in remaining_headers]
                if len(header)==2:columns=header;continue
                if columns and cells and cells[0] in remaining_headers:
                    for index,key in columns.items():
                        value=yen(row[index]) if len(row)>index else None
                        if value is not None and value.is_integer():_assign(out,key,{'year':year,'remaining':int(value)},'NISA枠の表示が重複しています。')
                    continue
                if len(indexes)==1:amount_index=indexes[0];continue
                if amount_index is not None and len(row)>amount_index:
                    keys=[frameworks[cell] for index,cell in enumerate(cells) if index!=amount_index and cell in frameworks]
                    value=yen(row[amount_index])
                    if len(keys)==1 and value is not None and value.is_integer():_assign(out,keys[0],{'year':year,'remaining':int(value)},'NISA枠の表示が重複しています。')
    if all(k in out for k in ('cash_total','broker_cash','bank')):
        from decimal import Decimal
        if Decimal(str(out['cash_total']))!=Decimal(str(out['broker_cash']))+Decimal(str(out['bank'])):
            raise ObservationConflict('現金合計と証券口座・連携銀行の内訳が一致しません。')
    if not out:raise LedgerError('口座サマリーの現金・NISA枠を確認できません。')
    return out

def parse_currency_summary(text,tables=(),required=True):
    """Read explicitly labelled USD cash and explicit JPY valuations only.

    The account(外貨建) screen may have buying-power rows beside cash balances.
    Those are deliberately excluded. Native USD is retained as usd_cash, never
    inserted into the Excel foreign_cash column, which is denominated in JPY.
    """
    from asset_snapshots import currency_money,money,normalized
    found={}
    usd_labels={'預り金(米ドル)','米ドル預り金','外貨預り金(米ドル)',
                '米ドル残高','預り金(USD)','外貨現金(米ドル)'}
    jpy_labels={'預り金(米ドル・円換算)','外貨預り金(円換算)',
                '米ドル預り金(円換算)','外貨現金(円)','預り金(米ドル)(円換算)'}
    for line,next_line in _summary_rows(text,tables):
        for label in usd_labels|jpy_labels:
            raw=next_line if line==label else line[len(label):] if line.startswith(label) else ''
            if not raw:continue
            if label in usd_labels:
                value=currency_money(raw,'USD',allow_bare=True)
                _assign(found,'usd_cash',value,'米ドル残高の表示が重複しています。')
                # Some My資産 layouts explicitly show this row in yen.
                _assign(found,'foreign_cash',money(raw),'外貨現金の円評価が重複しています。')
            else:_assign(found,'foreign_cash',money(raw,allow_bare=True),'外貨現金の円評価が重複しています。')
    # Currency rows select their column by the displayed heading. A bare value
    # in a USD-labelled balance column is USD; a yen valuation must say 円.
    usd_headers={'預り金(米ドル)','外貨預り金(米ドル)','預り金','預り金残高','外貨預り金','残高'}
    jpy_headers={'円換算残高','円換算額','円換算評価額','評価額(円)'}
    for rows in tables:
        cols={}
        for row in rows:
            hs=[normalized(x) for x in row]
            candidates={i:('usd_cash' if h in usd_headers else 'foreign_cash') for i,h in enumerate(hs) if h in usd_headers|jpy_headers}
            if candidates and any(h in ('通貨','通貨名') for h in hs):cols=candidates;continue
            if not cols or not any(h in ('米ドル','USD','米ドル(USD)') for h in hs):continue
            for index,key in cols.items():
                if len(row)<=index:continue
                value=currency_money(row[index],'USD',True) if key=='usd_cash' else money(row[index],True)
                _assign(found,key,value,'外貨建口座の残高表示が重複しています。')
    if found.get('usd_cash')==0 and 'foreign_cash' not in found:found['foreign_cash']=0.0
    if not found and required:raise LedgerError('米ドル現金の残高・通貨を確認できません。買付余力は採用しません。')
    return found

def parse_my_assets(text,tables):
    from asset_snapshots import extract_observations
    found=extract_observations(text,tables)
    # React layouts may use div rows, not HTML tables: first yen value after an exact category.
    from asset_snapshots import category,money
    lines=[x.strip() for x in unicodedata.normalize('NFKC',text).splitlines() if x.strip()]
    for i,line in enumerate(lines):
        k=category(norm(line))
        if k and i+1<len(lines):
            v=money(lines[i+1])
            if v is not None:
                if found.get(k) is not None and found[k]!=v:raise LedgerError('My資産の項目が重複しています。')
                found[k]=v
    native=parse_currency_summary(text,tables,required=False)
    for key,value in native.items():_assign(found,key,value,'My資産の通貨別残高が重複しています。')
    if not found:raise LedgerError('My資産の評価額を確認できません。')
    return found

def init_observations(store):
    store.db.execute('CREATE TABLE IF NOT EXISTS account_observations(account TEXT,kind TEXT,observed_at TEXT,payload TEXT,PRIMARY KEY(account,kind,observed_at))')
    store.db.commit()

def save_observation(store,account,kind,text,values):
    if account not in ('A','B','C','D'):raise LedgerError('自動資産取得はA～Dです。')
    # Observation time is NOT previous month end or an historical valuation timestamp.
    now=_jst_now().isoformat()
    stamps=re.findall(r'\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?',text)
    payload=dict(values=values,observed_at=now,displayed_at=stamps[0] if len(set(stamps))==1 else None,source=kind)
    init_observations(store)
    store.backup()
    with store.db:store.db.execute('INSERT INTO account_observations VALUES (?,?,?,?)',(account,kind,now,json.dumps(payload,ensure_ascii=False)))
    return payload

def latest(store,cutoff):
    if not store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_observations'").fetchone():return {}
    out={}
    for row in store.db.execute('SELECT * FROM account_observations ORDER BY observed_at'):
        if row['observed_at'][:10]<=cutoff:out[(row['account'],row['kind'])]=json.loads(row['payload'])
    return out

FIELD_LABELS={'domestic':'国内株式等','foreign':'外国株式(円評価)',
              'cash':'円現金合計','foreign_cash':'米ドル現金(円評価)',
              'growth_remaining':'NISA成長枠','tsumitate_remaining':'NISAつみたて枠'}

def _current_values(captured):
    from asset_snapshots import combine_assets
    asset=captured.get('My資産',{})
    current=combine_assets(asset)
    summary=captured.get('口座サマリー',{})
    if summary.get('cash_total') is not None:current['cash']=summary['cash_total']
    for key in ('growth_remaining','tsumitate_remaining'):
        value=summary.get(key,{})
        current[key]=value.get('remaining') if value.get('year')==_jst_now().year else None
    return current

def capture_status(store,account,cutoff=None,since=None):
    """Coverage for the given acquisition day, without treating old data as new."""
    day=cutoff or _jst_now().date().isoformat()
    observations=latest(store,day);captured={}
    for kind in ('口座サマリー','My資産'):
        item=observations.get((account,kind),{})
        stamp=item.get('observed_at','')
        if stamp[:10]==day and (since is None or stamp>=since):captured[kind]=item.get('values',{})
    values=_current_values(captured)
    available=[key for key in FIELD_LABELS if values.get(key) is not None]
    missing=[key for key in FIELD_LABELS if key not in available]
    usd=captured.get('My資産',{}).get('usd_cash') is not None
    return {'complete':not missing and usd,'available':available,'missing':missing,
            'usd_cash_observed':usd,'as_of':day}

def _check_cancelled(cancelled):
    if cancelled():raise CredentialError('中止しました。資産の取得を停止しました。')

def _check_asset_identity(page,owner,require_match=False):
    from auto_login import trusted
    if not trusted(page):raise CredentialError('SBI資産画面の移動先を確認できません。')
    if owner:
        from post_identity import state
        text,_=snapshot_dom(page)
        result=state(text,owner)
        if result=='MISMATCH':raise CredentialError('資産画面に別口座の名義を検出しました。保存せず停止しました。')
        if require_match and result!='MATCH':raise CredentialError('資産取得前の口座名義を確認できません。保存せず停止しました。')

def include_cash(page):
    """A local view filter, never an order or account-setting operation."""
    from auto_login import trusted
    for frame in frames(page):
        if not trusted(frame):continue
        controls=frame.get_by_role('checkbox',name='預り金を除外',exact=True)
        visible=[controls.nth(i) for i in range(controls.count()) if controls.nth(i).is_visible()]
        if len(visible)>1:raise CredentialError('預り金の表示切替を一意に確認できません。')
        if visible and visible[0].is_checked():
            visible[0].uncheck(timeout=5000);page.wait_for_timeout(500)

def reveal_asset_rows(page):
    """Use rendered display controls to reveal zero balances, never infer zero."""
    from auto_login import trusted
    changed=False
    for frame in frames(page):
        if not trusted(frame):continue
        for name in ('残高ゼロの項目を表示','残高0の項目を表示','残高のない商品を表示','残高ゼロを表示','残高0を表示'):
            loc=frame.get_by_role('checkbox',name=name,exact=True)
            matches=[loc.nth(index) for index in range(loc.count()) if loc.nth(index).is_visible()]
            if len(matches)>1:raise CredentialError('残高表示の切替を一意に確認できません。')
            if matches and not matches[0].is_checked():matches[0].check(timeout=5000);changed=True
        for name in ('資産の内訳を表示','内訳をすべて表示','資産をすべて表示'):
            loc=frame.get_by_role('button',name=name,exact=True)
            matches=[loc.nth(index) for index in range(loc.count()) if loc.nth(index).is_visible() and loc.nth(index).get_attribute('aria-expanded')=='false']
            if len(matches)>1:raise CredentialError('資産内訳の展開を一意に確認できません。')
            if matches:matches[0].click(timeout=5000);changed=True
    if changed:page.wait_for_timeout(400)


def capture_account(page,store,account,progress=lambda _:None,owner=None,cancelled=lambda:False,identity_verified=False,diagnostics=None,target_dates=()):
    if account not in ('A','B','C','D'):raise LedgerError('自動資産取得はA～Dです。')
    _check_cancelled(cancelled)
    _check_asset_identity(page,owner,require_match=bool(owner) and not identity_verified)
    captured={};asset_text='';asset_tables=[]
    stages=[('口座サマリー',['口座(円建)']),('My資産',['My資産','My資産トップ']),
            ('口座(外貨建)',['口座(外貨建)'])]
    for kind,names in stages:
        _check_cancelled(cancelled)
        try:
            if kind in ('口座サマリー','口座(外貨建)'):
                try:page=navigate(page,['口座管理'],timeout=5,cancelled=cancelled)
                except CredentialError:_check_cancelled(cancelled)
                try:page=navigate(page,names,timeout=5,cancelled=cancelled)
                except CredentialError:
                    _check_cancelled(cancelled)
                    # The current screen is acceptable only if its actual
                    # labelled balances parse below. Menu text alone is not.
                    pass
            else:
                try:page=navigate(page,names,timeout=5,cancelled=cancelled)
                except CredentialError:
                    _check_cancelled(cancelled)
                    from auto_login import trusted
                    if not trusted(page):raise
                    page.goto('https://site.sbisec.co.jp/account/assets',wait_until='domcontentloaded')
                include_cash(page)
                reveal_asset_rows(page)
            parser=parse_summary if kind=='口座サマリー' else parse_my_assets if kind=='My資産' else parse_currency_summary
            for attempt in range(12):
                _check_cancelled(cancelled);_check_asset_identity(page,owner)
                text,tables=snapshot_dom(page)
                try:
                    values=parser(text,tables)
                    if kind=='口座サマリー' and not any(k in values for k in ('cash_total','growth_remaining','tsumitate_remaining')):
                        raise LedgerError('口座サマリーの本体を確認できません。')
                    break
                except LedgerError:
                    if attempt==11:raise
                    page.wait_for_timeout(500)
            _check_cancelled(cancelled)
            save_observation(store,account,kind,text,values)
            captured[kind]=values
            progress(f'口座{account}：{kind}の表示値を取得しました（現在値）。')
            if kind=='My資産':
                asset_text,asset_tables=text,tables
                # Target-date collection uses only displayed-date history.
                # Monthly deltas are 1st 07:00 baselines and cannot establish
                # the previous calendar month-end or the beginning of day 1.
                if not target_dates:
                    from asset_snapshots import capture_history,capture_page,capture_month_start
                    try:
                        capture_history(store,account,tables)
                        capture_month_start(store,account,text,tables)
                        capture_page(store,account,text,tables)
                    except LedgerError:
                        progress(f'口座{account}：過去の指定日資産は未取得です。現在値では代用しません。')
                    # Keep the optional monthly-baseline workflow. It only uses
                    # explicitly labelled monthly-difference columns, never the
                    # nearby top-level daily P/L or a present-day balance.
                    _check_cancelled(cancelled)
                    try:
                        select_monthly_view(page)
                        monthly_text,monthly_tables=snapshot_dom(page)
                        capture_month_start(store,account,monthly_text,monthly_tables)
                    except (LedgerError,CredentialError):pass
        except CredentialError:
            # Identity mismatch and cancellation must never become a warning
            # followed by another page read or a saved snapshot.
            raise
        except Exception as error:
            progress(f'口座{account}：{kind}は未取得：{type(error).__name__}。')
            from pathlib import Path
            target=Path(diagnostics) if diagnostics is not None else Path(store.directory)/'diagnostics';target.mkdir(parents=True,exist_ok=True)
            stamp=_jst_now().strftime('%Y%m%d_%H%M%S')
            try:
                page.screenshot(path=str(target/f'assets_{account}_{stamp}.png'),full_page=False)
                text,tables=snapshot_dom(page)
                (target/f'assets_{account}_{stamp}.json').write_text(json.dumps({'kind':kind,'text':text,'tables':tables},ensure_ascii=False,indent=2),encoding='utf-8')
                progress('資産画面の診断を保存しました（口座情報を含みます）。')
            except Exception:pass
    # Merge only explicitly observed currency values. Keep the original USD
    # amount separate; a USD balance is never used as a yen balance.
    currency=captured.get('口座(外貨建)',{})
    if currency:
        assets=captured.setdefault('My資産',{})
        for key,value in currency.items():_assign(assets,key,value,'My資産と外貨建口座の残高が一致しません。')
        _check_cancelled(cancelled)
        save_observation(store,account,'My資産',asset_text,assets)
    current=_current_values(captured)
    if any(value is not None for value in current.values()):
        from asset_snapshots import save,snapshots,exact_fields
        stamp=_jst_now().date().isoformat();old=snapshots(store).get((account,stamp))
        if old and (not old.get('source','').startswith('SBI') or exact_fields(old)):
            progress('同日の手動・基準日確認済み資産記録を保持しました。現在値は取得時点の表で確認できます。')
        else:
            retained=False
            if old:
                for key in current:
                    if current[key] is None and old.get(key) is not None:current[key]=old[key];retained=True
            note='取得当日の表示値。過去日には代用しません。'+('一部は同日中の前回確認値。' if retained else '')
            _check_cancelled(cancelled)
            save(store,account,stamp,current,'SBI取得当日',note,replace=bool(old))
    # Report this run's coverage, not retained earlier observations.
    fresh=_current_values(captured)
    missing=[FIELD_LABELS[key] for key in FIELD_LABELS if fresh.get(key) is None]
    usd=captured.get('My資産',{}).get('usd_cash') is not None
    if not usd:missing.append('米ドル現金(原通貨)')
    if missing:progress(f'口座{account}：未取得項目＝'+ '、'.join(missing)+'。未確認の値は0にしません。')
    historical=capture_exact_dates(page,store,account,target_dates,progress,owner=owner,cancelled=cancelled) if target_dates else {}
    return not missing and all(item['complete'] for item in historical.values())


def select_monthly_view(page):
    text,tables=snapshot_dom(page)
    from asset_snapshots import normalized
    monthly_headers={'前月比(円)','前月比','前月比(率)','前月比/率(%)','前月比率(%)'}
    if any(any(normalized(cell) in monthly_headers for cell in row) for rows in tables for row in rows):return
    from auto_login import trusted
    from sbi_navigation import controls
    candidates=[]
    for frame in frames(page):
        if not trusted(frame):continue
        candidates+=controls(frame,['前月比','前月比(率)','前月比/率(%)'])
    if len(candidates)!=1:raise CredentialError('前月比の切替ボタンを確認できません。')
    # The 2026 My資産 view has separate 前日比/前月比 controls. Select the
    # requested monthly control directly; do not click the active daily tab.
    candidates[0].click(timeout=5000);page.wait_for_timeout(700)

# History controls are discovered from the rendered read-only UI. No hidden SBI
# endpoints or page-specific query parameters are invented here.
_DATE_CONTROLS='input:not([type="password"]),select'
_DATE_SCAN=r'''els=>els.map((el,index)=>{
 const r=el.getBoundingClientRect();
 return {index,tag:el.tagName.toLowerCase(),type:el.type||'',value:el.value||'',form:el.form?Array.from(document.forms).indexOf(el.form):null,
 labels:[el.getAttribute('aria-label'),el.getAttribute('placeholder'),...(el.labels?Array.from(el.labels).map(x=>{const c=x.cloneNode(true);c.querySelectorAll('input,select,textarea').forEach(n=>n.remove());return c.textContent;}):[])].filter(Boolean),
 visible:!!(r.width&&r.height&&getComputedStyle(el).visibility!=='hidden'),enabled:!el.disabled,
 options:el.tagName==='SELECT'?Array.from(el.options).map(x=>({value:x.value,label:x.textContent,disabled:x.disabled})):[]};
})'''
_HISTORY_SCOPES=r'''els=>els.filter(e=>e.getClientRects().length).map(e=>({
 text:e.innerText, label:[e.getAttribute('aria-label'),...Array.from(e.querySelectorAll(':scope > h1,:scope > h2,:scope > h3')).map(x=>x.innerText)].filter(Boolean).join(' '),
 tables:Array.from(e.querySelectorAll('table')).filter(t=>t.getClientRects().length).map(t=>Array.from(t.rows).map(r=>Array.from(r.cells).map(c=>c.innerText))),
 grids:Array.from(e.querySelectorAll('[role=table],[role=grid]')).filter(t=>t.getClientRects().length).map(t=>Array.from(t.querySelectorAll('[role=row]')).filter(r=>r.getClientRects().length).map(r=>Array.from(r.querySelectorAll('[role=columnheader],[role=rowheader],[role=cell],[role=gridcell]')).map(c=>c.innerText)))
}))'''

def _history_values(page,target):
    from auto_login import trusted
    from asset_snapshots import exact_table_values,exact_panel_values,displayed_asset_date,HistoricalConflict
    def record(key,value):
        if key in found and found[key]!=value:raise HistoricalConflict('指定日資産の表示領域どうしで数値が一致しません。')
        found[key]=value
    text,tables=snapshot_dom(page)
    found=exact_table_values(tables,target)
    scopes=[]
    for frame in frames(page):
        if not trusted(frame):continue
        for scope in frame.locator('section,article,[role="tabpanel"],[role="region"]').evaluate_all(_HISTORY_SCOPES):
            try:day=displayed_asset_date(scope['text'])
            except LedgerError:continue
            if day==target:scopes.append(scope)
    # A containing section must not attach an unrelated present-day NISA panel
    # to a smaller dated history panel merely because both share an ancestor.
    leaf_scopes=[scope for scope in scopes if not any(other is not scope and len(other['text'])<len(scope['text']) and other['text'] in scope['text'] for other in scopes)]
    for scope in leaf_scopes:
        history_label=scope.get('label','')
        allow_nisa=bool(re.search('指定日|履歴',history_label)) and not re.search('現在の?NISA|現在の投資可能|本日現在',scope['text'])
        values=exact_panel_values(scope['text'],scope['tables']+scope['grids'],target,allow_nisa=allow_nisa)
        for key,value in values.items():record(key,value)
    if not leaf_scopes:
        # A whole page may supply dated assets, but its year-only current NISA
        # summary is never considered historical evidence.
        try:values=exact_panel_values(text,tables,target,allow_nisa=False)
        except HistoricalConflict:raise
        except LedgerError:values={}
        for key,value in values.items():record(key,value)
    return found

def _history_busy(page):
    from auto_login import trusted
    for frame in frames(page):
        if not trusted(frame):continue
        busy=frame.locator('[aria-busy="true"],[role="progressbar"]')
        if any(busy.nth(index).is_visible() for index in range(busy.count())):return True
    return False

def _date_control_kind(row):
    labels={norm(label).replace('を選択','').rstrip(':：') for label in row['labels']}
    if labels&{'開始日','表示開始日','期間開始日','対象期間開始日'}:return 'start'
    if labels&{'終了日','表示終了日','期間終了日','対象期間終了日'}:return 'end'
    if labels&{'基準日','評価基準日','評価日','対象日','日付','指定日','表示日','対象年月日'} or row['type']=='date':return 'day'
    if row['tag']=='select' and labels&{'年','対象年','表示年'}:return 'year'
    if row['tag']=='select' and labels&{'月','対象月','表示月'}:return 'month'
    if row['tag']=='select' and labels&{'日','対象日(号)','表示日(号)'}:return 'day_number'
    return None

def _set_history_date(page,target,cancelled=lambda:False):
    text,_=snapshot_dom(page)
    if not re.search('My資産|資産推移|資産履歴|資産残高履歴|残高履歴|指定日資産',text):
        raise LedgerError('指定日を照会する資産画面であることを確認できません。')
    from auto_login import trusted
    from asset_snapshots import source_date
    groups={key:[] for key in ('day','start','end','year','month','day_number')}
    for frame in frames(page):
        if not trusted(frame):continue
        for row in frame.locator(_DATE_CONTROLS).evaluate_all(_DATE_SCAN):
            if not row['visible'] or not row['enabled']:continue
            kind=_date_control_kind(row)
            # A select containing explicit date choices is itself observed proof
            # of the available dates; numeric values alone are not dates.
            if row['tag']=='select' and kind is None:
                if any(re.fullmatch(r'\d{4}[/-]\d{1,2}[/-]\d{1,2}',str(option['value'])) for option in row['options']):kind='day'
            if kind:groups[kind].append((frame,row))
    selected=[]
    if len(groups['start'])==len(groups['end'])==1 and not groups['day']:
        selected=groups['start']+groups['end']
    elif len(groups['day'])==1 and not groups['start'] and not groups['end']:
        selected=groups['day']
    elif all(len(groups[key])==1 for key in ('year','month','day_number')) and not groups['day']:
        selected=groups['year']+groups['month']+groups['day_number']
    else:raise LedgerError('指定日資産の表示日コントロールを一意に確認できません。')
    if len({id(frame) for frame,_ in selected})>1 or len({row.get('form') for _,row in selected if row.get('form') is not None})>1:
        raise LedgerError('指定日の入力欄が別の照会領域に分かれています。')
    elements=[]
    for frame,row in selected:
        _check_cancelled(cancelled)
        element=frame.locator(_DATE_CONTROLS).nth(row['index']);elements.append(element)
        if row['tag']=='select':
            choices=[];kind=_date_control_kind(row)
            for option in row['options']:
                if option['disabled']:continue
                if kind in ('year','month','day_number'):
                    index={'year':0,'month':1,'day_number':2}[kind]
                    expected=int(target.split('-')[index]);label=norm(option['label']).rstrip('年月日')
                    if label.isdigit() and int(label)==expected:choices.append(option['value'])
                else:
                    for candidate in (option['label'],option['value']):
                        try:match=source_date(candidate)==target
                        except LedgerError:match=False
                        if match:choices.append(option['value']);break
            if len(choices)!=1:raise LedgerError('指定日の選択肢を一意に確認できません。')
            element.select_option(value=choices[0],timeout=5000)
        else:
            if row['type']=='date':value=target
            elif '/' in row['value'] or any('/' in label for label in row['labels']):value=target.replace('-','/')
            elif re.fullmatch(r'\d{4}-\d{1,2}-\d{1,2}',row['value']):value=target
            else:raise LedgerError('指定日入力欄の日付書式を確認できません。')
            element.fill(value,timeout=5000);element.press('Tab',timeout=5000)
    # Only observed query controls in the date field's own form/region. Never
    # click account changes, trades, or an unrelated update button elsewhere.
    scopes=[]
    for element in elements:
        scope=element.locator('xpath=ancestor::form[1]')
        if scope.count()!=1:scope=element.locator('xpath=ancestor::*[self::section or self::article or @role="region" or @role="tabpanel"][1]')
        if scope.count()==1:scopes.append(scope)
    buttons=[]
    if scopes:
        scope=scopes[0]
        for name in ('表示','照会','検索','適用'):
            loc=scope.get_by_role('button',name=name,exact=True)
            buttons.extend(loc.nth(index) for index in range(loc.count()) if loc.nth(index).is_visible() and loc.nth(index).is_enabled())
    if len(buttons)>1:raise LedgerError('指定日照会ボタンを一意に確認できません。')
    if buttons:
        _check_cancelled(cancelled);buttons[0].click(timeout=5000)
    return True

def capture_exact_dates(page,store,account,target_dates,progress=lambda _:None,owner=None,cancelled=lambda:False,timeout=12):
    """Best-effort read-only exact-date navigation; missing data stays missing."""
    from asset_snapshots import parse_date,merge_exact,historical_status,HistoricalConflict
    from auto_login import trusted
    targets=tuple(dict.fromkeys(parse_date(day) for day in target_dates))
    if not targets:return {}
    _check_cancelled(cancelled);_check_asset_identity(page,owner)
    # Current tables might already contain explicitly dated history rows.
    for target in targets:
        _check_cancelled(cancelled)
        try:
            values={} if _history_busy(page) else _history_values(page,target)
            if values:merge_exact(store,account,target,values,observed_at=_jst_now().isoformat())
        except HistoricalConflict:raise
        except LedgerError:
            progress(f'口座{account}：{target}の表示値が一意でないため既存値を保持しました。')
    status=historical_status(store,account,targets)
    pending=[target for target in targets if not status[target]['complete']]
    if pending:
        ready=False
        try:
            text,_=snapshot_dom(page)
            ready=bool(re.search('My資産|資産推移|資産履歴|資産残高履歴|残高履歴|指定日資産',text)) and any(row['visible'] and row['enabled'] and _date_control_kind(row) for frame in frames(page) if trusted(frame) for row in frame.locator(_DATE_CONTROLS).evaluate_all(_DATE_SCAN))
        except Exception:pass
        if not ready:
            try:
                page=navigate(page,['My資産','My資産トップ'],timeout=5,cancelled=cancelled)
            except CredentialError:_check_cancelled(cancelled)
            try:
                page=navigate(page,['資産推移','資産履歴','資産残高履歴','残高履歴'],timeout=5,cancelled=cancelled)
            except CredentialError:_check_cancelled(cancelled)
    for target in pending:
        _check_cancelled(cancelled);_check_asset_identity(page,owner)
        try:
            values={} if _history_busy(page) else _history_values(page,target)
            if values:merge_exact(store,account,target,values,observed_at=_jst_now().isoformat())
            if historical_status(store,account,(target,))[target]['complete']:continue
            _set_history_date(page,target,cancelled=cancelled)
            deadline=time.monotonic()+timeout;last=None;stable_since=None;best={}
            while time.monotonic()<deadline:
                _check_cancelled(cancelled);_check_asset_identity(page,owner)
                if _history_busy(page):last=None;stable_since=None;page.wait_for_timeout(250);continue
                values=_history_values(page,target)
                signature=json.dumps(values,sort_keys=True)
                if values and signature==last:
                    if time.monotonic()-stable_since>=1:
                        best=values;break
                else:last=signature;stable_since=time.monotonic()
                page.wait_for_timeout(300)
            if best:
                _check_cancelled(cancelled)
                merge_exact(store,account,target,best,observed_at=_jst_now().isoformat())
        except (CredentialError,HistoricalConflict):raise
        except LedgerError:
            progress(f'口座{account}：{target}の指定日表示を確認できません。現在値では補いません。')
        status=historical_status(store,account,(target,))[target]
        missing=[FIELD_LABELS[key] for key in status['missing']]
        progress(f'口座{account}：{target} '+('指定日資産・NISAを確認しました。' if not missing else '未取得＝'+'、'.join(missing)+'。'))
    return historical_status(store,account,targets)
