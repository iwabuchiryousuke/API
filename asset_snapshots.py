"""Exact-date observations, independent of transactions. Unknown is NULL, not zero."""
import json
from datetime import datetime
from decimal import Decimal
from ledger import LedgerError,parse_date

class HistoricalConflict(LedgerError):
    """Displayed exact-date evidence contradicts another confirmed value."""
    pass

KEYS=('domestic','foreign','cash','foreign_cash')
def init(store):
    store.db.execute('CREATE TABLE IF NOT EXISTS asset_snapshots(account TEXT NOT NULL REFERENCES accounts(id),as_of TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(account,as_of))');store.db.commit()
def snapshots(store):
    init(store)
    return {(r['account'],r['as_of']):json.loads(r['payload']) for r in store.db.execute('SELECT * FROM asset_snapshots')}
def save(store,account,as_of,values,source='手動確認',note='',replace=False,evidence=None):
    init(store);as_of=parse_date(as_of)
    if account not in ('A','B','C','D'):raise LedgerError('父の資産は管理Excelへ手入力してください。')
    payload={}
    for k in KEYS+('growth_remaining','tsumitate_remaining'):
        v=values.get(k)
        if v in ('',None):payload[k]=None;continue
        try:x=Decimal(str(v).replace(',',''))
        except Exception:raise LedgerError('資産額は数値または空欄です。')
        if not x.is_finite() or x<0:raise LedgerError('資産額には0以上の有限数値を入力してください。')
        payload[k]=float(x)
    payload.update(source=source,note=note,recorded_at=datetime.now().astimezone().isoformat())
    if evidence is not None:payload['evidence']=evidence
    previous=store.db.execute('SELECT payload FROM asset_snapshots WHERE account=? AND as_of=?',(account,as_of)).fetchone()
    if previous and not replace:
        old=json.loads(previous['payload'])
        if all(old.get(k)==payload[k] for k in KEYS+('growth_remaining','tsumitate_remaining')) and old.get('source')==source and old.get('note','')==note and old.get('evidence')==evidence:return False
        raise LedgerError('同じ日付の資産額が登録済みで内容が異なります。上書きを避けて停止しました。')
    store.backup()
    with store.db:store.db.execute('INSERT OR REPLACE INTO asset_snapshots VALUES (?,?,?)',(account,as_of,json.dumps(payload,ensure_ascii=False)))
    return True

def parse_asset_text(text):
    """Strict SBI displayed JPY totals only; do not infer from buying power.
    Site variants without these exact labels require manual confirmation.
    """
    import re,unicodedata
    text=unicodedata.normalize('NFKC',text)
    labels={'domestic':'国内株式','foreign':'米国株式','cash':'預り金(円)','foreign_cash':'預り金(米ドル・円換算)'}
    result={}
    for key,label in labels.items():
        hits=re.findall(r'(?m)^\s*'+re.escape(label)+r'\s*\n?\s*([\d,]+(?:\.\d+)?)\s*円\s*$',text)
        if len(hits)!=1:raise LedgerError('資産画面の項目・円換算・重複を確定できません。資産額の手動確認を使ってください。')
        result[key]=float(Decimal(hits[0].replace(',','')))
    return result


# v3.4: accept labelled DOM rows instead of assuming one fixed line layout.
import re,unicodedata
ALIASES={
 'domestic':('国内株式','国内株式(現物)','国内株式等'),
 'foreign':('米国株式','米国株式(現物)','外国株式(円)','米国株式(円換算)','外国株式','外国株式(現物)','外国株式(米国)','外国株式(米国株式)'),
 'cash':('預り金(円)','円預り金','SBI証券口座分'),
 'foreign_cash':('預り金(米ドル・円換算)','預り金(米ドル)','外貨現金(円)','外貨預り金(円換算)','米ドル預り金(円換算)','預り金(米ドル)(円換算)'),
 'bank':('SBIハイパー預金残高','ハイブリッド預金','SBIハイブリッド預金','スィープ専用銀行口座','スイープ専用銀行口座'),
 'fund':('投資信託','投資信託(国内)'),
 'bond':('円貨建債券',),
 'usd_fund':('外貨建MMF','外貨建MMF(米ドル建て)'),
 'usd_bond':('外貨建債券(米ドル建て)',),
}
def normalized(s):return re.sub(r'\s+','',unicodedata.normalize('NFKC',str(s)))
def category(label):
    n=normalized(label)
    return next((key for key,labels in ALIASES.items() if n in map(normalized,labels)),None)
# A currency marker or a labelled table column is required. Signs, percentages,
# buying power and dollar values are never accepted as a JPY asset balance.
NUMBER=r'(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?'
def money(value,allow_bare=False):
    v=normalized(value)
    match=re.fullmatch('('+NUMBER+r')(円)?',v)
    if not match or (not match[2] and not allow_bare):return None
    result=Decimal(match[1].replace(',',''))
    return float(result) if result.is_finite() else None

def currency_money(value,currency,allow_bare=False):
    """Native amounts stay separate from the workbook's JPY-valued columns."""
    v=normalized(value)
    if currency=='JPY':return money(value,allow_bare)
    if currency!='USD':return None
    match=re.fullmatch(r'(?:USD|US\$|\$)?('+NUMBER+r')(米ドル|USD|US\$|ドル)?',v,re.I)
    if not match or ('USD' not in v.upper() and '$' not in v and 'ドル' not in v and not allow_bare):return None
    result=Decimal(match[1].replace(',',''))
    return float(result) if result.is_finite() else None

def cell_money(value,allow_bare=False):
    """One explicit yen amount in a value cell, possibly beside native USD.
    Multiple yen amounts or signed P/L lines are ambiguous and are rejected.
    """
    raw=[x.strip() for x in str(value).splitlines() if x.strip()]
    if len(raw)<=1:return money(value,allow_bare)
    values=[money(x) for x in raw]
    values=[x for x in values if x is not None]
    return values[0] if len(values)==1 else None

def combine_assets(observed):
    def add(base,extras):
        if observed.get(base) is None or any(k in observed and observed[k] is None for k in extras):return None
        return observed[base]+sum(observed.get(k,0) for k in extras)
    result=dict(domestic=add('domestic',('fund','bond')),foreign=add('foreign',('usd_fund','usd_bond')),foreign_cash=observed.get('foreign_cash'))
    # Bank is never silently treated as zero. Two independently labelled balances
    # are required to report combined cash.
    result['cash']=observed['cash']+observed['bank'] if observed.get('cash') is not None and observed.get('bank') is not None else None
    return result

def extract_observations(text,tables=()):
    found={}
    def record(key,value):
        # Navigation labels and an unrelated following label are not evidence.
        # An unknown field remains absent until a displayed numeric row exists.
        if value is None:return
        if key in found and found[key]!=value:raise LedgerError('資産額の同名項目に異なる数値があります。')
        found[key]=value
    lines=[line.strip() for line in unicodedata.normalize('NFKC',text).splitlines() if line.strip()]
    for i,line in enumerate(lines):
        key=category(line)
        if key and i+1<len(lines):record(key,money(lines[i+1]))
        for k,labels in ALIASES.items():
            for label in labels:
                m=re.fullmatch(re.escape(label)+r'\s+('+NUMBER+r'\s*円)',line)
                if m:record(k,money(m[1]))
    for rows in tables:
        if not rows:continue
        value_index=None;is_jpy=False
        for row in rows:
            header=[normalized(x) for x in row]
            indexes=[i for i,h in enumerate(header) if h in ('評価額(円)','資産残高(円)','円換算評価額','評価額','円換算額')]
            if len(indexes)==1:
                value_index=indexes[0];is_jpy=header[value_index]!='評価額';continue
            if value_index is None or len(row)<=value_index:continue
            keys=[category(x) for i,x in enumerate(row) if i!=value_index and category(x)]
            if len(keys)!=1:continue
            record(keys[0],cell_money(row[value_index],is_jpy))
    return found

def capture_page(store,account,text,tables=()):
    body=unicodedata.normalize('NFKC',text)
    dates=re.findall(r'(?:評価基準日|基準日時|資産評価日|更新日時)[ :：\n]*(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})',body)
    if not dates:dates=re.findall(r'(\d{4})/(\d{1,2})/(\d{1,2})\s+\d{1,2}:\d{2}',body)
    if len(set(dates))!=1:raise LedgerError('画面の評価基準日・更新日を特定できません。')
    y,m,d=map(int,dates[0]);as_of=f'{y:04}-{m:02}-{d:02}'
    observed=extract_observations(body,tables);values=combine_assets(observed)
    if not any(v is not None for v in values.values()):raise LedgerError('円評価額を確認できません。ドル額や買付余力は採用しません。')
    missing=[k for k in KEYS if values[k] is None]
    note='SBI表示の円評価。'+('未取得：'+','.join(missing) if missing else '預り金＋別表示の連携銀行残高。')
    save(store,account,as_of,values,'SBI My資産',note)
    return 1

def capture_history(store,account,tables):
    """Only explicitly dated category totals in a history table are accepted."""
    count=0
    names={'国内株式等(円)':'domestic','外国株式(円)':'foreign','現金合計(円)':'cash','外貨現金(円)':'foreign_cash'}
    for rows in tables:
        columns={};day_index=None
        for row in rows:
            normalized_row=[normalized(v) for v in row]
            if '日付' in normalized_row:
                day_index=normalized_row.index('日付');columns={i:names[h] for i,h in enumerate(normalized_row) if h in names};continue
            if day_index is None or not columns or len(row)<=max([day_index]+list(columns)):continue
            try:dt=parse_date(row[day_index])
            except Exception:continue
            values={k:money(row[i],True) for i,k in columns.items()}
            if any(v is not None for v in values.values()):
                save(store,account,dt,values,'SBI My資産履歴','画面の日付別・円建て項目');count+=1
    return count

def capture_browser(store,account,page):
    texts=[];tables=[]
    from auto_login import trusted
    for frame in page.frames:
        if not trusted(frame):continue
        try:
            texts.append(frame.locator('body').inner_text(timeout=5000))
            tables.extend(frame.locator('table').evaluate_all("els => els.filter(e=>e.getClientRects().length).map(t=>Array.from(t.rows).map(r=>Array.from(r.cells).map(c=>c.innerText)))"))
        except Exception:continue
    count=capture_history(store,account,tables)
    count+=capture_month_start(store,account,'\n'.join(texts),tables)
    try:count+=capture_page(store,account,'\n'.join(texts),tables)
    except LedgerError:
        if not count:raise
    return count

def capture_month_start(store,account,text,tables):
    """SBI defines monthly difference against day 1 at 07:00.
    Only explicitly labelled yen differences can reconstruct that baseline.
    """
    body=unicodedata.normalize('NFKC',text)
    dates=re.findall(r'(?:評価基準日|基準日時|資産評価日|更新日時)[ :：\n]*(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})',body)
    if not dates:dates=re.findall(r'(\d{4})/(\d{1,2})/(\d{1,2})\s+\d{1,2}:\d{2}',body)
    if len(set(dates))!=1:return 0
    observed={}
    for rows in tables:
        value_index=delta_index=None;bare=False
        for row in rows:
            hs=[normalized(x) for x in row]
            vi=[i for i,h in enumerate(hs) if h in ('評価額(円)','資産残高(円)','評価額')]
            di=[i for i,h in enumerate(hs) if h in ('前月比(円)','前月比','前月比(率)','前月比/率(%)','前月比率(%)')]
            if len(vi)==len(di)==1:value_index,delta_index=vi[0],di[0];bare=hs[value_index]!='評価額';continue
            if value_index is None or len(row)<=max(value_index,delta_index):continue
            keys=[category(x) for i,x in enumerate(row) if i not in (value_index,delta_index) and category(x)]
            if len(keys)!=1:continue
            now=money(row[value_index].splitlines()[0],bare);raw=normalized(row[delta_index].splitlines()[0]).replace('−','-').replace('△','-')
            match=re.fullmatch(r'([+-]?[0-9][0-9,]*(?:\.[0-9]+)?)(?:円)?',raw)
            if now is None or not match:continue
            baseline=now-float(Decimal(match[1].replace(',','')))
            if baseline<0:continue
            if keys[0] in observed and observed[keys[0]]!=baseline:raise LedgerError('前月比から計算した基準額が一致しません。')
            observed[keys[0]]=baseline
    values=combine_assets(observed)
    if not any(v is not None for v in values.values()):return 0
    y,m,_=map(int,dates[0]);dt=f'{y:04}-{m:02}-01'
    save(store,account,dt,values,'SBI My資産の前月比','当月1日07:00基準＝評価額−前月比。未確認項目は空欄。')
    return 1

# Exact-date history is deliberately separate from the monthly-difference
# baseline. A first-day 07:00 derivation does not establish a calendar boundary.
SNAPSHOT_FIELDS=KEYS+('growth_remaining','tsumitate_remaining')
_HISTORY_COLUMNS={
 '国内株式等(円)':'domestic','国内資産合計(円)':'domestic',
 '外国株式(円)':'foreign','外国資産合計(円)':'foreign',
 '現金合計(円)':'cash','現金残高等(合計)(円)':'cash',
 '外貨現金(円)':'foreign_cash','外貨預り金(円換算)':'foreign_cash',
 '成長枠残高(円)':'growth_remaining','成長投資枠残高(円)':'growth_remaining',
 'NISA成長投資枠残高(円)':'growth_remaining','NISA(成長投資枠)投資可能額(円)':'growth_remaining',
 '積立枠残高(円)':'tsumitate_remaining','つみたて投資枠残高(円)':'tsumitate_remaining',
 'NISAつみたて投資枠残高(円)':'tsumitate_remaining','NISA(つみたて投資枠)投資可能額(円)':'tsumitate_remaining',
}
_DATE_COLUMNS={'日付','基準日','評価基準日','資産評価日','年月日','対象日'}

def source_date(value):
    raw=unicodedata.normalize('NFKC',str(value)).strip()
    match=re.fullmatch(r'(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})(?:日)?(?:\s*[（(][月火水木金土日][）)])?',raw)
    if not match:raise LedgerError('表示日付を特定できません。')
    return parse_date('%04d-%02d-%02d'%tuple(map(int,match.groups())))

def displayed_asset_date(text):
    """A displayed source date, never an input's requested value."""
    text=unicodedata.normalize('NFKC',text)
    dates=[]
    for match in re.finditer(r'(?:評価基準日|基準日時|資産評価日|残高基準日|対象日|基準日)[\s:：]*(\d{4}[年/-]\d{1,2}[月/-]\d{1,2}日?)',text):
        dates.append(source_date(match[1]))
    for match in re.finditer(r'(\d{4}[年/-]\d{1,2}[月/-]\d{1,2}日?)\s*(?:時点|現在の資産)',text):
        dates.append(source_date(match[1]))
    # A My資産 timestamp is a displayed valuation timestamp. If an explicit
    # heading and timestamp disagree, keep the ambiguity instead of picking one.
    for match in re.finditer(r'(\d{4}/\d{1,2}/\d{1,2})\s+\d{1,2}:\d{2}(?::\d{2})?',text):
        dates.append(source_date(match[1]))
    if len(set(dates))!=1:raise LedgerError('指定日資産の表示基準日を一意に確認できません。')
    return dates[0]

def exact_table_values(tables,target_date):
    """Only date-bound rows and explicitly JPY-denominated total columns."""
    target=parse_date(target_date);found={}
    for rows in tables:
        day_index=None;columns={}
        for row in rows:
            header=[normalized(cell) for cell in row]
            indexes=[i for i,cell in enumerate(header) if cell in _DATE_COLUMNS]
            if len(indexes)==1:
                day_index=indexes[0];columns={i:_HISTORY_COLUMNS[cell] for i,cell in enumerate(header) if cell in _HISTORY_COLUMNS}
                continue
            if day_index is None or not columns or len(row)<=max([day_index]+list(columns)):continue
            try:day=source_date(row[day_index])
            except LedgerError:continue
            if day!=target:continue
            for index,key in columns.items():
                value=cell_money(row[index],True)
                if value is None:continue
                if key in found and found[key]!=value:raise HistoricalConflict('同じ基準日の履歴行に異なる資産額があります。')
                found[key]=value
    return found

def exact_panel_values(text,tables,target_date,allow_nisa=False):
    """A single displayed-date panel. It must not combine a current summary
    outside the panel with historical assets inside it. Browser acquisition
    passes a scoped dated region, never the whole mixed screen, for NISA.
    """
    target=parse_date(target_date)
    if displayed_asset_date(text)!=target:raise LedgerError('表示基準日が指定日と一致しません。現在値を過去日へ保存しません。')
    found={}
    # Labelled total rows in a date-scoped panel, including div layouts.
    lines=[line.strip() for line in unicodedata.normalize('NFKC',text).splitlines() if line.strip()]
    for index,line in enumerate(lines):
        key=_HISTORY_COLUMNS.get(normalized(line))
        if key in ('growth_remaining','tsumitate_remaining') and not allow_nisa:continue
        if key and index+1<len(lines):
            value=money(lines[index+1],True)
            if value is not None:
                if key in found and found[key]!=value:raise HistoricalConflict('指定日領域の資産額が重複しています。')
                found[key]=value
    # Tables may have product/value rows rather than dates across columns.
    try:raw=extract_observations(text,tables)
    except LedgerError as error:raise HistoricalConflict('指定日領域の商品別資産額が一致しません。') from error
    if all(raw.get(key) is not None for key in ('domestic','fund','bond')):
        found.setdefault('domestic',raw['domestic']+raw['fund']+raw['bond'])
    if all(raw.get(key) is not None for key in ('foreign','usd_fund','usd_bond')):
        found.setdefault('foreign',raw['foreign']+raw['usd_fund']+raw['usd_bond'])
    if raw.get('cash') is not None and raw.get('bank') is not None:found.setdefault('cash',raw['cash']+raw['bank'])
    if raw.get('foreign_cash') is not None:found.setdefault('foreign_cash',raw['foreign_cash'])
    if allow_nisa:
        from sbi_views import parse_summary,ObservationConflict
        try:summary=parse_summary(text,tables)
        except ObservationConflict as error:
            raise HistoricalConflict('指定日領域のNISA・現金表示が一致しません。') from error
        except LedgerError:summary={}
        for key in ('growth_remaining','tsumitate_remaining'):
            value=summary.get(key,{})
            if value.get('year')==int(target[:4]):found[key]=value['remaining']
    return found

def exact_fields(payload):
    if not payload:return set()
    evidence=payload.get('evidence',{})
    if evidence.get('method')=='displayed-date':return set(evidence.get('fields',()))&set(SNAPSHOT_FIELDS)
    # These legacy sources were explicitly dated. Acquisition-day and derived
    # monthly baselines are intentionally excluded.
    if payload.get('source') in ('手動確認','SBI My資産履歴','SBI My資産'):
        return {key for key in SNAPSHOT_FIELDS if payload.get(key) is not None}
    return set()

def merge_exact(store,account,as_of,values,source='SBI 指定日資産',note='表示基準日を直接確認。',observed_at=None):
    """Add exact evidence without clearing manual or separately verified data."""
    as_of=parse_date(as_of)
    incoming={key:values[key] for key in SNAPSHOT_FIELDS if values.get(key) is not None}
    if not incoming:return False
    old=snapshots(store).get((account,as_of));fields=exact_fields(old)
    for key,value in incoming.items():
        if key in fields and old.get(key)!=value:raise HistoricalConflict('同じ基準日の確認済み資産と取得値が一致しません。既存値を保持しました。')
    if old and not old.get('source','').startswith('SBI'):
        # The user's dated manual record owns this row, including its blanks.
        return False
    merged={key:(old.get(key) if old and key in fields else None) for key in SNAPSHOT_FIELDS}
    for key,value in incoming.items():merged[key]=value
    if old and set(incoming)<=fields and all(old.get(key)==value for key,value in incoming.items()):return False
    evidence={'method':'displayed-date','as_of':as_of,'fields':sorted(fields|set(incoming))}
    if observed_at is not None:evidence['observed_at']=observed_at
    return save(store,account,as_of,merged,source,note,replace=bool(old),evidence=evidence)

def historical_status(store,account,target_dates):
    rows=snapshots(store);result={}
    for target in target_dates:
        target=parse_date(target);payload=rows.get((account,target));fields=exact_fields(payload)
        available=[key for key in SNAPSHOT_FIELDS if key in fields and payload.get(key) is not None]
        missing=[key for key in SNAPSHOT_FIELDS if key not in available]
        result[target]={'complete':not missing,'available':available,'missing':missing,
                        'source':payload.get('source') if payload else None}
    return result
