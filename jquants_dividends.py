"""JPX J-Quants V2 public dividends. Never substitutes for personal receipts."""
import json,math,re,time
from datetime import date,timedelta,datetime
from urllib.request import Request,urlopen
from urllib.parse import urlencode
from urllib.error import HTTPError,URLError

class MarketError(ValueError):pass

def get_rows(path,params,key,opener=urlopen):
    if path not in ('fins/dividend','markets/calendar'):raise MarketError('未対応APIです。')
    params=dict(params);rows=[];seen=set()
    for _ in range(100):
        req=Request('https://api.jquants.com/v2/'+path+'?'+urlencode(params),headers={'x-api-key':key})
        for attempt in range(3):
            try:
                with opener(req,timeout=30) as response:data=json.load(response)
                break
            except HTTPError as e:
                if e.code in (429,500,502,503,504) and attempt<2:time.sleep(2**attempt);continue
                if e.code in (401,403):raise MarketError('J-QuantsのAPIキー・契約範囲を確認してください。配当金情報APIはPremium対象です。') from None
                raise MarketError('J-Quants APIの応答エラーです。') from None
            except (URLError,TimeoutError):raise MarketError('J-Quants APIへ接続できません。') from None
        if not isinstance(data,dict) or not isinstance(data.get('data'),list):raise MarketError('J-Quantsの応答形式を確認できません。')
        if not all(isinstance(r,dict) for r in data['data']):raise MarketError('J-Quants明細形式が不正です。')
        rows+=data['data'];cursor=data.get('pagination_key')
        if not cursor:return rows
        if not isinstance(cursor,str) or cursor in seen:raise MarketError('J-Quantsのページングが循環しています。')
        seen.add(cursor);params['pagination_key']=cursor
    raise MarketError('J-Quants取得上限に達しました。未完成のデータは適用しません。')

def date_or_none(x):
    try:return date.fromisoformat(x).isoformat()
    except (ValueError,TypeError):return None

def rate(x):
    try:v=float(x)
    except (ValueError,TypeError):return None
    return v if math.isfinite(v) and v>=0 else None

def active_notices(rows,code):
    records={}
    for r in rows:
        if str(r.get('Code')) not in (code,code+'0'):continue
        ref=str(r.get('RefNo',''))
        if not ref or str(r.get('StatCode')) not in ('1','2','3'):raise MarketError('配当通知の識別情報が不足しています。')
        if ref in records and records[ref]!=r:raise MarketError('同じ配当通知に異なる内容があります。')
        records[ref]=r
    def root(ref):
        visited=set()
        while ref in records:
            if ref in visited:raise MarketError('配当訂正の参照が循環しています。')
            visited.add(ref);r=records[ref];parent=str(r.get('CARefNo') or ref)
            if str(r['StatCode'])=='1' or parent==ref:return ref
            ref=parent
        return ref
    groups={}
    for ref,r in records.items():groups.setdefault(root(ref),[]).append(r)
    active=[]
    for entries in groups.values():
        entries.sort(key=lambda r:(r.get('PubDate',''),r.get('PubTime','')))
        r=entries[-1]
        if len(entries)>1 and (entries[-2].get('PubDate'),entries[-2].get('PubTime'))==(r.get('PubDate'),r.get('PubTime')):raise MarketError('配当訂正の前後関係を特定できません。')
        if str(r['StatCode'])!='3':active.append(r)
    # Forecast and decided notices are separate series; never sum both.
    groups={}
    for r in active:
        rec=date_or_none(r.get('RecDate'))
        if not rec:continue
        groups.setdefault((rec,str(r.get('IFCode',''))),[]).append(r)
    result=[]
    for entries in groups.values():
        decided=[r for r in entries if str(r.get('FRCode'))=='1']
        selected=decided or [r for r in entries if str(r.get('FRCode'))=='2']
        if not selected:raise MarketError('配当の予想・決定区分を確認できません。')
        selected.sort(key=lambda r:(r.get('PubDate',''),r.get('PubTime','')))
        if len(selected)>1 and (selected[-2].get('PubDate'),selected[-2].get('PubTime'))==(selected[-1].get('PubDate'),selected[-1].get('PubTime')):raise MarketError('配当通知が競合しています。')
        result.append(selected[-1])
    return result

def last_rights(ex_date,calendar):
    ex=date_or_none(ex_date)
    if not ex:return None
    # Require continuous calendar coverage up to the previous trading day.
    days={r.get('Date'):str(r.get('HolDiv')) for r in calendar};d=date.fromisoformat(ex)-timedelta(days=1)
    for _ in range(20):
        v=days.get(d.isoformat())
        if v in ('1','2'):return d.isoformat()
        if v not in ('0','3'):return None
        d-=timedelta(days=1)
    return None

def fetch(code,year,key,request=get_rows):
    code=str(code).upper()
    if not re.fullmatch(r'[0-9][0-9A-Z]{3}',code):raise MarketError('国内上場銘柄の4文字コードが必要です。')
    # Full accessible history is required to resolve corrections and cancellations.
    raw=request('fins/dividend',{'code':code},key)
    rows=active_notices(raw,code)
    calendar=request('markets/calendar',{'from':f'{year-1}-12-01','to':f'{year+1}-12-31'},key)
    events=[]
    for r in rows:
        rec=date_or_none(r.get('RecDate'));pay=date_or_none(r.get('PayDate'));rights=last_rights(r.get('ExDate'),calendar)
        if not (rec and rec[:4]==str(year) or pay and pay[:4]==str(year) or rights and rights[:4]==str(year)):continue
        events.append(dict(rights=rights,payment=pay,dps=rate(r.get('DivRate')),record_date=rec,ex_date=date_or_none(r.get('ExDate')),decision='決定' if str(r.get('FRCode'))=='1' else '予想',reference=str(r['RefNo'])))
    relevant=[e for e in events if (e['rights'] or e['record_date'])[:4]==str(year)]
    return dict(events=events,rights=' / '.join(sorted({e['rights'] for e in relevant if e['rights']})),payments=' / '.join(sorted({e['payment'] for e in relevant if e['payment']})),dps=None,calendar_complete=False,source='JPX J-Quants /fins/dividend + /markets/calendar',status='API取得済み。各回DPSは予定明細。年間DPSは分割調整確認後に入力。未公表予定は未確認。',fetched_at=datetime.now().astimezone().isoformat())
