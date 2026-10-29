"""One committed, owner-bound batch of current cash and NISA observations.

The original observations remain untouched. A batch never borrows a missing
field from another acquisition, a historical balance, or another currency.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import math
import re
import unicodedata

from acquisition_journal import _observation
from ledger import LedgerError

JST=timezone(timedelta(hours=9),'JST')
TABLE='current_acquisitions'
FIELDS=('cash_total','usd_cash','growth_remaining','tsumitate_remaining')
FIELD_LABELS={'cash_total':'円現金','usd_cash':'米ドル現金','growth_remaining':'NISA成長枠','tsumitate_remaining':'NISAつみたて枠'}
_KINDS=('口座サマリー','口座(外貨建)')
_BATCH=re.compile(r'[A-Za-z0-9_-]{1,100}\Z')
_SCHEMA='sbi-current-acquisition'
_MAX_BYTES=128*1024


class CurrentCaptureError(LedgerError):
    pass


def _error(message='現在値の取得記録の形式・整合性を確認できません。'):
    return CurrentCaptureError(message+' 既存の台帳と取得記録は保持しています。')


def _stamp(value):
    try:
        stamp=value if isinstance(value,datetime) else datetime.fromisoformat(value)
        if stamp.tzinfo is None or stamp.utcoffset() is None:raise ValueError()
        return stamp.astimezone(JST)
    except (TypeError,ValueError,OverflowError):raise _error('取得時刻のタイムゾーンを確認できません。') from None


def _owner(value):
    if not isinstance(value,str) or not value.strip() or len(value)>50:raise _error('口座のPDF名義を確認してください。')
    name=''.join(c for c in unicodedata.normalize('NFKC',value) if not c.isspace() and unicodedata.category(c)!='Cf')
    if not name or any(unicodedata.category(c)=='Cc' for c in name):raise _error('口座名義を確認できません。')
    return name


def _number(value):
    if value is None:return None
    if type(value) not in (int,float) or not math.isfinite(value) or value<0:raise _error('現在値の金額を確認できません。')
    return value


def _canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')


def _digest(value):
    return hashlib.sha256(_canonical({key:item for key,item in value.items() if key!='sha256'})).hexdigest()


def init_current_capture(store,commit=True):
    store.db.execute('CREATE TABLE IF NOT EXISTS current_acquisitions(batch_id TEXT PRIMARY KEY,captured_at TEXT,payload TEXT NOT NULL)')
    if commit:store.db.commit()


def observation_identities(store,account):
    """Capture the exact rows present before a single account acquisition."""
    if not store.db.execute("SELECT 1 FROM sqlite_master WHERE name='account_observations'").fetchone():return set()
    return {tuple(row) for row in store.db.execute(
        'SELECT kind,observed_at,payload FROM account_observations WHERE account=? AND kind IN (?,?)',
        (account,*_KINDS))}


def account_capture(store,account,owner,before,since,until):
    """Read only fresh rows inside this account's actual acquisition window."""
    if account not in 'ABCD' or len(account)!=1:raise _error('現在値取得の対象口座はA～Dです。')
    owner=_owner(owner);since=_stamp(since);until=_stamp(until)
    if until<since:raise _error('取得開始と終了の時刻が逆転しています。')
    latest={}
    if store.db.execute("SELECT 1 FROM sqlite_master WHERE name='account_observations'").fetchone():
        rows=list(store.db.execute(
                'SELECT kind,observed_at,payload FROM account_observations WHERE account=? AND kind IN (?,?)',
                (account,*_KINDS)))
        if not set(before)<={tuple(row) for row in rows}:raise _error('登録済みの取得観測値が変更されています。')
        for row in rows:
            if tuple(row) in before:continue
            try:item=_observation(dict(kind=row['kind'],observed_at=row['observed_at'],payload=json.loads(row['payload'])),strict=True)
            except (ValueError,TypeError,KeyError):raise _error('今回の取得観測値を確認できません。') from None
            stamp=_stamp(item['observed_at'])
            if not since<=stamp<=until:raise _error('今回の取得観測値が実行時間の範囲外です。')
            previous=latest.get(item['kind'])
            if previous is None or stamp>_stamp(previous['observed_at']):latest[item['kind']]=item
    values={};sources={}
    summary=latest.get('口座サマリー');foreign=latest.get('口座(外貨建)')
    if summary:
        source=summary['payload']['values'];stamp=_stamp(summary['observed_at'])
        if source.get('cash_total') is not None:values['cash_total']=_number(source['cash_total'])
        for key in ('growth_remaining','tsumitate_remaining'):
            item=source.get(key)
            if item is not None and item['year']==stamp.year and item.get('remaining') is not None:
                values[key]=dict(year=item['year'],remaining=_number(item['remaining']))
        sources['口座サマリー']=summary['observed_at']
    if foreign:
        source=foreign['payload']['values']
        if source.get('usd_cash') is not None:values['usd_cash']=_number(source['usd_cash'])
        sources['口座(外貨建)']=foreign['observed_at']
    observed=max(sources.values(),key=_stamp) if sources else None
    return dict(owner=owner,observed_at=observed,values=values,
                missing=[key for key in FIELDS if key not in values],source_observed_at=sources)


def _validate_account(value):
    if not isinstance(value,dict) or set(value)!={'owner','observed_at','values','missing','source_observed_at'}:raise _error()
    owner=_owner(value['owner']);observed=value['observed_at'];sources=value['source_observed_at']
    if not isinstance(sources,dict) or set(sources)-set(_KINDS):raise _error()
    normalized={kind:_stamp(stamp).isoformat() for kind,stamp in sources.items()}
    if normalized!=sources:raise _error('画面別の取得日時を確認できません。')
    if observed is not None and _stamp(observed).isoformat()!=observed:raise _error()
    if observed!=(max(sources.values(),key=_stamp) if sources else None):raise _error('取得日時と画面の記録が一致しません。')
    values=value['values']
    if not isinstance(values,dict) or set(values)-set(FIELDS):raise _error()
    out={}
    for key in ('cash_total','usd_cash'):
        if key in values:
            if values[key] is None:raise _error('未取得の値を数値として保存できません。')
            out[key]=_number(values[key])
    if 'cash_total' in out and '口座サマリー' not in sources:raise _error('円現金の取得画面を確認できません。')
    if 'usd_cash' in out and '口座(外貨建)' not in sources:raise _error('米ドル元額の取得画面を確認できません。')
    for key in ('growth_remaining','tsumitate_remaining'):
        if key not in values:continue
        item=values[key]
        if (not isinstance(item,dict) or set(item)!={'year','remaining'} or type(item['year']) is not int
                or '口座サマリー' not in sources or item['year']!=_stamp(sources['口座サマリー']).year
                or item['remaining'] is None):raise _error('NISA枠の対象年・残額を確認できません。')
        out[key]=dict(year=item['year'],remaining=_number(item['remaining']))
    missing=[key for key in FIELDS if key not in out]
    if value['missing']!=missing:raise _error('取得済み・未取得項目の判定が一致しません。')
    return dict(owner=owner,observed_at=observed,values=out,missing=missing,source_observed_at=normalized)


def save_current_capture(store,batch_id,accounts,attempted_at):
    """Append one staged batch; caller commits it with the reviewed transaction."""
    if not isinstance(batch_id,str) or not _BATCH.fullmatch(batch_id):raise _error('取得識別子を確認できません。')
    if not isinstance(accounts,dict) or set(accounts)!=set('ABCD'):raise _error('現在値はA～Dの同じ取得処理で保存してください。')
    profiles={row['id']:row for row in store.accounts()}
    accepted={}
    for account,value in accounts.items():
        value=_validate_account(value)
        if _owner(profiles.get(account,{}).get('owner_name'))!=value['owner']:raise _error('取得中に口座名義が変更されました。')
        accepted[account]=value
    moments=[item['observed_at'] for item in accepted.values() if item['observed_at'] is not None]
    captured=max(moments,key=_stamp) if moments else None
    value=dict(schema=_SCHEMA,version=1,batch_id=batch_id,attempted_at=_stamp(attempted_at).isoformat(),
               captured_at=captured,accounts=accepted)
    value['sha256']=_digest(value)
    init_current_capture(store)
    previous=store.db.execute('SELECT payload FROM current_acquisitions WHERE batch_id=?',(batch_id,)).fetchone()
    if previous:
        if previous[0]==_canonical(value).decode('utf-8'):return value
        raise _error('同じ取得処理の記録は上書きできません。')
    with store.db:
        store.db.execute('INSERT INTO current_acquisitions VALUES (?,?,?)',
                         (batch_id,captured,_canonical(value).decode('utf-8')))
    return value


def load_current_capture(store):
    """Latest committed attempt only; missing data never falls back to older rows."""
    if not store.db.execute("SELECT 1 FROM sqlite_master WHERE name='current_acquisitions'").fetchone():return None
    row=store.db.execute('SELECT batch_id,captured_at,payload FROM current_acquisitions ORDER BY rowid DESC LIMIT 1').fetchone()
    if row is None:return None
    raw=row['payload']
    if not isinstance(raw,str) or len(raw.encode('utf-8'))>_MAX_BYTES:raise _error()
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise _error()
            result[key]=value
        return result
    try:value=json.loads(raw,object_pairs_hook=unique)
    except (ValueError,TypeError,RecursionError):raise _error('現在値の保存記録を読み取れません。') from None
    required={'schema','version','batch_id','attempted_at','captured_at','accounts','sha256'}
    try:checksum=_digest(value) if isinstance(value,dict) else ''
    except (TypeError,ValueError,OverflowError):raise _error('現在値の数値・形式を確認できません。') from None
    if (not isinstance(value,dict) or set(value)!=required or value['schema']!=_SCHEMA or type(value['version']) is not int
            or value['version']!=1 or value['batch_id']!=row['batch_id'] or not _BATCH.fullmatch(value['batch_id'])
            or value['captured_at']!=row['captured_at'] or not isinstance(value['sha256'],str)
            or not hmac.compare_digest(value['sha256'],checksum)):raise _error()
    if _stamp(value['attempted_at']).isoformat()!=value['attempted_at']:raise _error()
    if not isinstance(value['accounts'],dict) or set(value['accounts'])!=set('ABCD'):raise _error()
    profiles={entry['id']:entry for entry in store.accounts()}
    for account,item in value['accounts'].items():
        accepted=_validate_account(item)
        if accepted!=item or accepted['owner']!=_owner(profiles.get(account,{}).get('owner_name')):raise _error('現在の口座名義と取得記録が一致しません。')
    moments=[item['observed_at'] for item in value['accounts'].values() if item['observed_at'] is not None]
    if value['captured_at']!=(max(moments,key=_stamp) if moments else None):raise _error()
    return value
