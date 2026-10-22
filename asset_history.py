"""Practical daily observations, explicitly separate from exact boundary values.

This module uses only already accepted account_observations. It never logs in,
queries an invented SBI endpoint, or reconstructs a missed day from holdings.
Legacy displayed_at can be a page clock: it is not proof of valuation time.
"""
from datetime import date,datetime,timedelta,timezone
from decimal import Decimal
import hashlib
import json
import math
from uuid import uuid4

from ledger import LedgerError


JST=timezone(timedelta(hours=9),'JST')
FIELDS=('domestic','foreign','cash','foreign_cash','growth_remaining','tsumitate_remaining')
KINDS=('口座サマリー','My資産','口座(外貨建)')


class HistoryError(LedgerError):pass


def _day(value):
    try:
        if not isinstance(value,str) or date.fromisoformat(value).isoformat()!=value:raise ValueError()
        return value
    except (ValueError,TypeError):raise HistoryError('日付はYYYY-MM-DDで指定してください。') from None


def _stamp(value):
    try:
        result=value if isinstance(value,datetime) else datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() is None:raise ValueError()
        return result.astimezone(JST)
    except (ValueError,TypeError,OverflowError):raise HistoryError('資産履歴の取得時刻を確認できません。タイムゾーンを含む時刻が必要です。') from None


def _account(account):
    if account not in tuple('ABCD'):raise HistoryError('自動取得履歴の口座はA～Dです。父の口座は手動記録を保持します。')


def _number(value):
    if value is None:return None
    if isinstance(value,bool) or not isinstance(value,(int,float,Decimal)):raise HistoryError('資産履歴の金額が数値ではありません。')
    try:result=float(value)
    except (OverflowError,ValueError):raise HistoryError('資産履歴の金額が0以上の有限数値ではありません。') from None
    if not math.isfinite(result) or result<0:raise HistoryError('資産履歴の金額が0以上の有限数値ではありません。')
    return result


def _total(values,parts):
    # Missing rows are not zero, even when another component is displayed.
    numbers=[_number(values.get(key)) for key in parts]
    return _number(sum(numbers)) if all(value is not None for value in numbers) else None


def _values(kind,raw,year):
    if not isinstance(raw,dict):raise HistoryError('資産履歴の表示値を読み取れません。')
    out={key:None for key in FIELDS};warnings=[]
    if kind=='My資産':
        out['domestic']=_total(raw,('domestic','fund','bond'))
        out['foreign']=_total(raw,('foreign','usd_fund','usd_bond'))
        out['cash']=_number(raw.get('cash_total')) if raw.get('cash_total') is not None else _total(raw,('cash','bank'))
    if kind=='口座サマリー':
        out['cash']=_number(raw.get('cash_total')) if raw.get('cash_total') is not None else _total(raw,('broker_cash','bank'))
        for key in ('growth_remaining','tsumitate_remaining'):
            item=raw.get(key)
            if item is None:continue
            if not isinstance(item,dict) or type(item.get('year')) is not int:raise HistoryError('資産履歴のNISA枠の対象年を確認できません。')
            value=_number(item.get('remaining'))
            if item['year']==year:out[key]=value
            else:warnings.append('NISA_YEAR_DIFFERS:'+key)
    if kind in ('My資産','口座(外貨建)'):out['foreign_cash']=_number(raw.get('foreign_cash'))
    return out,_number(raw.get('usd_cash')),warnings


def _displayed(value,observed_day):
    if value is None:return None,'absent'
    if not isinstance(value,str):return None,'invalid-clock'
    parsed=None
    for fmt in ('%Y/%m/%d %H:%M:%S','%Y/%m/%d %H:%M'):
        try:parsed=datetime.strptime(value,fmt);break
        except ValueError:pass
    if parsed is None:
        try:parsed=datetime.fromisoformat(value)
        except ValueError:return None,'invalid-clock'
        if parsed.tzinfo is not None:parsed=parsed.astimezone(JST)
    day=parsed.date().isoformat()
    return day,('same-day-clock' if day==observed_day else 'different-day-clock')


def daily_records(store,account,day):
    """Read original observations for an actual JST day, without DB writes.

    Each record keeps its own acquisition time and displayed clock. No merge
    occurs here. usd_cash remains native USD and never enters a JPY column.
    Invalid acquisition timestamps or amount payloads fail closed.
    """
    _account(account);day=_day(day)
    exists=store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_observations'").fetchone()
    if not exists:return []
    result=[]
    for row in store.db.execute('SELECT kind,observed_at,payload FROM account_observations WHERE account=?',(account,)):
        observed=_stamp(row['observed_at'])
        if observed.date().isoformat()!=day:continue
        try:payload=json.loads(row['payload'])
        except (ValueError,TypeError):raise HistoryError('資産履歴の保存内容を読み取れません。') from None
        if not isinstance(payload,dict) or row['kind'] not in KINDS or payload.get('source')!=row['kind'] or _stamp(payload.get('observed_at'))!=observed:
            raise HistoryError('資産履歴の取得元・取得時刻が一致しません。')
        values,usd,warnings=_values(row['kind'],payload.get('values'),observed.year)
        displayed=payload.get('displayed_at');displayed_day,status=_displayed(displayed,day)
        if status in ('different-day-clock','invalid-clock'):warnings.append('SOURCE_CLOCK_NOT_SAME_DAY')
        raw={'account':account,'kind':row['kind'],'observed_at':observed.isoformat(),'displayed_at':displayed,'values':values,'usd_cash':usd}
        record_id=hashlib.sha256(json.dumps(raw,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()
        result.append(dict(raw,record_id=record_id,observed_day=day,displayed_day=displayed_day,
            source_date_status=status,displayed_at_is_valuation=False,
            available=[key for key in FIELDS if values[key] is not None],
            missing=[key for key in FIELDS if values[key] is None],warnings=warnings))
    return sorted(result,key=lambda record:(_stamp(record['observed_at']),record['kind'],record['record_id']))


def _windows(account,windows):
    found={}
    for window in windows or ():
        if not isinstance(window,dict):raise HistoryError('取得バッチの時刻を確認できません。')
        if window.get('account')!=account:continue
        start=_stamp(window.get('since'));end=_stamp(window.get('acquired_at'))
        if end<start:raise HistoryError('取得バッチの終了時刻が開始時刻より前です。')
        found[(start,end)]={'account':account,'since':start.isoformat(),'acquired_at':end.isoformat()}
    return found


def _role(day):
    value=date.fromisoformat(day)
    if value.day==1:return 'first-observed-in-day'
    if (value+timedelta(days=1)).day==1:return 'last-observed-in-day'
    return None


def _groups(records,windows):
    groups={};standalone={}
    for record in records:
        stamp=_stamp(record['observed_at'])
        memberships=[key for key in windows if key[0]<=stamp<=key[1]]
        if len(memberships)==1:groups.setdefault(memberships[0],[]).append(record)
        else:
            warnings=['AMBIGUOUS_ACQUISITION_WINDOW'] if memberships else []
            key=(record['kind'],stamp)
            bucket=standalone.setdefault(key,([],None,warnings));bucket[0].append(record)
    return [(items,windows[key],[]) for key,items in groups.items()]+list(standalone.values())


def _measurement(records,window,role,warnings):
    # Asset observations anchor the window; summary/currency rows are selected
    # only from this same validated batch and this same calendar day.
    priority=next((kind for kind in ('My資産','口座サマリー','口座(外貨建)') if any(r['kind']==kind for r in records)),None)
    anchors=[r for r in records if r['kind']==priority]
    select=min if role=='first-observed-in-day' else max
    anchor_stamp=select(_stamp(r['observed_at']) for r in anchors)
    selected=[]
    for kind in KINDS:
        candidates=[r for r in records if r['kind']==kind]
        if not candidates:continue
        distance=min(abs((_stamp(r['observed_at'])-anchor_stamp).total_seconds()) for r in candidates)
        nearest=[r for r in candidates if abs((_stamp(r['observed_at'])-anchor_stamp).total_seconds())==distance]
        chosen=select(_stamp(r['observed_at']) for r in nearest)
        selected.extend(r for r in nearest if _stamp(r['observed_at'])==chosen)
    values={key:None for key in FIELDS};usd=None;fields={};conflicts=[]
    for key in FIELDS+('usd_cash',):
        evidence=[r for r in selected if (r['usd_cash'] if key=='usd_cash' else r['values'][key]) is not None]
        numbers={(r['usd_cash'] if key=='usd_cash' else r['values'][key]) for r in evidence}
        if len(numbers)>1:conflicts.append(key);continue
        if not numbers:continue
        value=next(iter(numbers))
        if key=='usd_cash':usd=value
        else:values[key]=value
        fields[key]={'value':value,'observations':[{'record_id':r['record_id'],'kind':r['kind'],'observed_at':r['observed_at'],'displayed_at':r['displayed_at']} for r in evidence]}
    times=[_stamp(r['observed_at']) for r in selected]
    return {'anchor_kind':priority,'anchor_at':anchor_stamp.isoformat(),'records':selected,
        'values':values,'usd_cash':usd,'fields':fields,'conflicts':conflicts,
        'available':[key for key in FIELDS if values[key] is not None],
        'missing':[key for key in FIELDS if values[key] is None],
        'observed_from':min(times).isoformat(),'observed_to':max(times).isoformat(),
        'coherence':'journal-window' if window else 'single-observation',
        'acquisition_window':window,'warnings':sorted(set(warnings+[w for r in selected for w in r['warnings']]))}


def boundary_candidates(store,account,target_dates,acquisition_windows=()):
    """Return first/last *observations*, never exact opening/closing balances.

    Multi-kind measurements require an explicit validated acquisition window
    from journal replay. Nearness of timestamps alone never joins observations.
    A record whose displayed clock has another date is conservatively excluded.
    """
    _account(account);windows=_windows(account,acquisition_windows);result={}
    for raw_day in target_dates:
        day=_day(raw_day);role=_role(day);all_records=daily_records(store,account,day)
        eligible=[r for r in all_records if r['source_date_status'] not in ('different-day-clock','invalid-clock')]
        base={'account':account,'as_of':day,'selection':role,'exact_boundary':False,
            'displayed_at_is_valuation':False,'first_record':all_records[0] if all_records else None,
            'last_record':all_records[-1] if all_records else None,
            'values':{key:None for key in FIELDS},'usd_cash':None,'records':[],
            'available':[],'missing':list(FIELDS),'fields':{},'conflicts':[],
            'warnings':[],'observed_from':None,'observed_to':None,
            'coherence':None,'acquisition_window':None}
        if not role:base['warnings'].append('NOT_A_CALENDAR_MONTH_BOUNDARY')
        else:
            measurements=[_measurement(items,window,role,warnings) for items,window,warnings in _groups(eligible,windows)]
            measurements=[item for item in measurements if item['available'] or item['usd_cash'] is not None or item['conflicts']]
            if measurements:
                # A USD-only record cannot displace an independently usable
                # JPY/NISA partial candidate for the existing workbook columns.
                useful=[item for item in measurements if item['available'] or item['conflicts']]
                if useful:measurements=useful
                # Prefer a real asset observation; summary-only observations
                # remain a useful partial fallback, not a fabricated total.
                rank={'My資産':0,'口座サマリー':1,'口座(外貨建)':2}
                priority=min(rank[item['anchor_kind']] for item in measurements)
                measurements=[item for item in measurements if rank[item['anchor_kind']]==priority]
                select=min if role=='first-observed-in-day' else max
                chosen=select(measurements,key=lambda item:(_stamp(item['anchor_at']),item['observed_from'],item['observed_to']))
                base.update(chosen)
            if len(eligible)<len(all_records):base['warnings'].append('DIFFERENT_OR_INVALID_SOURCE_DAY_EXCLUDED')
            if not eligible:base['warnings'].append('NO_SAME_DAY_OBSERVATION')
        result[day]=base
    return result


def preserve_snapshot(payload):
    source=str(payload.get('source',''))
    evidence=payload.get('evidence')
    return not source.startswith('SBI') or source in {'SBI My資産','SBI My資産履歴','SBI 指定日資産'} or (isinstance(evidence,dict) and evidence.get('method')=='displayed-date')


def _note(candidate):
    start=_stamp(candidate['observed_from']);end=_stamp(candidate['observed_to'])
    span=start.strftime('%H:%M:%S')+(('–'+end.strftime('%H:%M:%S')) if end!=start else '')
    return '当日実測 '+span+' JST（月末終値・月初開始時点ではない）。取得時刻ベース。表示時計の評価基準は未確認。'


def stage_boundary_observations(store,account,target_dates,acquisition_windows=()):
    """Add labelled same-day measurements to a WORKFLOW STAGING store only.

    The caller owns review/commit to the live ledger. This does not change the
    workbook layout. Manual and displayed-date rows, including blanks, win.
    """
    candidates=boundary_candidates(store,account,target_dates,acquisition_windows)
    result={'count':0,'rows':[],'proposal':[],'warnings':[],'preserved':[]}
    table=store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='asset_snapshots'").fetchone()
    existing={}
    if table:
        for row in store.db.execute('SELECT as_of,payload FROM asset_snapshots WHERE account=?',(account,)):
            try:payload=json.loads(row['payload'])
            except (ValueError,TypeError):raise HistoryError('既存の資産記録を読み取れません。既存値を保持して停止しました。') from None
            if not isinstance(payload,dict):raise HistoryError('既存の資産記録を確認できません。既存値を保持して停止しました。')
            existing[row['as_of']]=payload
    planned=[]
    for day,candidate in candidates.items():
        result['warnings'].extend('DAILY_OBSERVATION:'+account+':'+day+':'+w for w in candidate['warnings'])
        old=existing.get(day)
        if old is not None and preserve_snapshot(old):
            result['preserved'].append(day);continue
        if candidate['conflicts']:
            result['warnings'].append('DAILY_OBSERVATION_CONFLICT:'+account+':'+day+':'+','.join(candidate['conflicts']));continue
        if not candidate['available']:continue
        evidence={'method':'observed-at','as_of':day,'role':candidate['selection'],
            'observed_from':candidate['observed_from'],'observed_to':candidate['observed_to'],
            'coherence':candidate['coherence'],'fields':candidate['available'],
            'observation_ids':sorted(r['record_id'] for r in candidate['records']),
            'displayed_at_is_valuation':False}
        payload=dict(candidate['values'],source='SBI 当日実測',note=_note(candidate),evidence=evidence)
        if old and all(old.get(key)==payload.get(key) for key in FIELDS+('source','note','evidence')):continue
        payload['recorded_at']=datetime.now(JST).isoformat()
        planned.append((day,payload))
    if not planned:return result
    savepoint='daily_measurements_'+uuid4().hex
    store.db.execute('SAVEPOINT '+savepoint)
    try:
        store.db.execute('CREATE TABLE IF NOT EXISTS asset_snapshots(account TEXT NOT NULL REFERENCES accounts(id),as_of TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(account,as_of))')
        for day,payload in planned:
            store.db.execute('INSERT OR REPLACE INTO asset_snapshots VALUES (?,?,?)',(account,day,json.dumps(payload,ensure_ascii=False,allow_nan=False)))
        store.db.execute('RELEASE '+savepoint)
    except BaseException:
        store.db.execute('ROLLBACK TO '+savepoint);store.db.execute('RELEASE '+savepoint);raise
    for day,payload in planned:
        result['count']+=1;result['rows'].append({'account':account,'as_of':day,'payload':payload})
        result['proposal'].append({'account':account,'kind':'月境界の当日実測','state':'当日実測を採用（終値・開始時点ではない）','name':day})
    return result
