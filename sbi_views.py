"""Read-only observed SBI navigation. Verify owner at electronic post before saving."""
import re,time,unicodedata,json
from datetime import datetime,timezone,timedelta
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError
from ledger import LedgerError
from login_credentials import CredentialError

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
    if key in out and out[key]!=value:raise LedgerError(message)
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
    if all(k in out for k in ('cash_total','broker_cash','bank')):
        from decimal import Decimal
        if Decimal(str(out['cash_total']))!=Decimal(str(out['broker_cash']))+Decimal(str(out['bank'])):
            raise LedgerError('現金合計と証券口座・連携銀行の内訳が一致しません。')
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

def capture_account(page,store,account,progress=lambda _:None,owner=None,cancelled=lambda:False,identity_verified=False,diagnostics=None):
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
        from asset_snapshots import save,snapshots
        stamp=_jst_now().date().isoformat();old=snapshots(store).get((account,stamp))
        if old and not old.get('source','').startswith('SBI'):
            progress('同日の手動資産記録を保持しました。自動取得値は取得時点の表で確認できます。')
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
    return not missing


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
