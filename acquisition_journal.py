"""Durable, account-bound asset evidence; replay changes only a staging store.

This is a checksum for corruption detection, not a digital signature. Journals
contain financial values and an owner binding, so they stay with private data.
Never persist a raw screen, OAuth setting, credential, or executable content.
"""
from datetime import date,datetime,timedelta,timezone
from decimal import Decimal
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import tempfile
import unicodedata
import uuid

from asset_snapshots import KEYS,exact_fields
from ledger import LedgerError


class JournalError(LedgerError):
    pass


_JST=timezone(timedelta(hours=9))
_KINDS={'口座サマリー','My資産','口座(外貨建)'}
_FIELDS=KEYS+('growth_remaining','tsumitate_remaining')
_AMOUNTS=set(KEYS)|{'bank','fund','bond','usd_fund','usd_bond','usd_cash','cash_total','broker_cash','summary_stocks','summary_total'}
_SOURCES={'SBI My資産','SBI My資産履歴','SBI My資産の前月比','SBI取得当日','SBI 指定日資産'}
_TOP={'schema','version','account','owner','acquired_at','since','target_dates','observations','snapshots','sha256'}
_MAX_BYTES=4*1024*1024


def _error(message='取得証跡の形式・整合性を確認できません。'):
    return JournalError(message+' 原本を保持して停止しました。data/acquisitionsを確認してください。')


def _stamp(value):
    try:
        stamp=value if isinstance(value,datetime) else datetime.fromisoformat(value)
        if stamp.tzinfo is None or stamp.utcoffset() is None:raise ValueError()
        return stamp.astimezone(timezone.utc)
    except (TypeError,ValueError,OverflowError):raise _error('取得証跡の取得時刻を確認できません。') from None


def _iso(value):return _stamp(value).astimezone(_JST).isoformat()


def _day(value):
    try:
        if not isinstance(value,str) or date.fromisoformat(value).isoformat()!=value:raise ValueError()
        return value
    except (TypeError,ValueError):raise _error('取得証跡の基準日を確認できません。') from None


def _owner(value):
    if not isinstance(value,str) or not value.strip() or len(value)>50:raise _error('取得証跡の名義設定を確認できません。')
    result=''.join(c for c in unicodedata.normalize('NFKC',value) if not c.isspace() and unicodedata.category(c)!='Cf')
    if not result:raise _error('取得証跡の名義設定を確認できません。')
    return result


def _number(value):
    if value is None:return None
    if isinstance(value,bool) or not isinstance(value,(int,float,Decimal)):raise _error('取得証跡の数値を確認できません。')
    try:value=float(value)
    except (ValueError,TypeError,OverflowError):raise _error('取得証跡の数値を確認できません。') from None
    if not math.isfinite(value) or value<0:raise _error('取得証跡の数値を確認できません。')
    return value


def _values(values,strict=False):
    if not isinstance(values,dict):raise _error()
    if strict and set(values)-(_AMOUNTS|{'growth_remaining','tsumitate_remaining'}):raise _error()
    out={key:_number(value) for key,value in values.items() if key in _AMOUNTS}
    for key in ('growth_remaining','tsumitate_remaining'):
        if key not in values:continue
        item=values[key]
        if not isinstance(item,dict) or set(item)!={'year','remaining'}:raise _error()
        year=item['year']
        if type(year) is not int or not 1900<=year<=9999:raise _error()
        out[key]={'year':year,'remaining':_number(item['remaining'])}
    return out


def _observation(item,strict=False):
    if not isinstance(item,dict) or set(item)!={'kind','observed_at','payload'}:raise _error()
    kind=item['kind'];payload=item['payload']
    if not isinstance(kind,str) or kind not in _KINDS or not isinstance(payload,dict):raise _error()
    if strict and set(payload)!={'values','observed_at','displayed_at','source'}:raise _error()
    stamp=_iso(item['observed_at'])
    if _stamp(payload.get('observed_at'))!=_stamp(stamp) or payload.get('source')!=kind:raise _error()
    displayed=payload.get('displayed_at')
    if displayed is not None:
        valid=False
        if isinstance(displayed,str):
            for fmt in ('%Y/%m/%d %H:%M','%Y/%m/%d %H:%M:%S'):
                try:datetime.strptime(displayed,fmt);valid=True;break
                except ValueError:pass
        if not valid:
            if strict:raise _error()
            displayed=None
    payload={'values':_values(payload.get('values'),strict),'observed_at':stamp,
             'displayed_at':displayed,'source':kind}
    return {'kind':kind,'observed_at':stamp,'payload':payload}


def _snapshot(item,strict=False):
    if not isinstance(item,dict) or set(item)!={'as_of','payload'}:raise _error()
    day=_day(item['as_of']);payload=item['payload']
    if not isinstance(payload,dict) or not isinstance(payload.get('source'),str) or payload['source'] not in _SOURCES:raise _error()
    if strict and set(payload)-set(_FIELDS)-{'source','recorded_at','evidence'}:raise _error()
    out={key:_number(payload.get(key)) for key in _FIELDS}
    out.update(source=payload['source'],recorded_at=_iso(payload.get('recorded_at')))
    if 'evidence' in payload:
        evidence=payload['evidence']
        if not isinstance(evidence,dict) or evidence.get('method')!='displayed-date' or _day(evidence.get('as_of'))!=day:raise _error()
        if strict and set(evidence)-{'method','as_of','fields','observed_at'}:raise _error()
        fields=evidence.get('fields')
        if not isinstance(fields,list) or any(not isinstance(key,str) for key in fields) or len(set(fields))!=len(fields) or any(key not in _FIELDS or out[key] is None for key in fields):raise _error()
        out['evidence']={'method':'displayed-date','as_of':day,'fields':sorted(fields)}
        if 'observed_at' in evidence:out['evidence']['observed_at']=_iso(evidence['observed_at'])
    return {'as_of':day,'payload':out}


def _canonical(data):return json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
def _digest(data):return hashlib.sha256(_canonical({k:v for k,v in data.items() if k!='sha256'})).hexdigest()


def _settings(store,account,owner=None):
    if account not in tuple('ABCD'):raise _error('取得証跡の口座はA～Dです。')
    configured=next((a.get('owner_name','') for a in store.accounts() if a['id']==account),'')
    if owner is not None and _owner(configured)!=_owner(owner):raise _error('取得証跡と口座設定の名義が一致しません。')
    return configured


def persist_capture(base,account,owner,store,since,target_dates=()):
    """Call only after this account's identity-verified, uncancelled capture.

    Extract fresh fields from the staging database without modifying it. A failed
    later account or a discarded temporary ledger cannot delete this evidence.
    """
    _settings(store,account,owner);start=_stamp(since)
    tables={r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    observations=[];snapshots=[]
    if 'account_observations' in tables:
        for row in store.db.execute('SELECT kind,observed_at,payload FROM account_observations WHERE account=?',(account,)):
            if _stamp(row['observed_at'])<start:continue
            try:item=_observation({'kind':row['kind'],'observed_at':row['observed_at'],'payload':json.loads(row['payload'])})
            except (ValueError,TypeError):raise _error() from None
            observations.append(item)
    if 'asset_snapshots' in tables:
        for row in store.db.execute('SELECT as_of,payload FROM asset_snapshots WHERE account=?',(account,)):
            try:payload=json.loads(row['payload'])
            except (ValueError,TypeError):raise _error() from None
            if not isinstance(payload,dict):raise _error()
            if not str(payload.get('source','')).startswith('SBI'):continue
            if _stamp(payload.get('recorded_at'))<start:continue
            snapshots.append(_snapshot({'as_of':row['as_of'],'payload':payload}))
    observations.sort(key=lambda v:(v['kind'],v['observed_at']))
    if not observations and not snapshots:return None
    times=[start,datetime.now(timezone.utc)]
    times.extend(_stamp(v['observed_at']) for v in observations)
    times.extend(_stamp(v['payload']['recorded_at']) for v in snapshots)
    data={'schema':'sbi-family-acquisition','version':1,'account':account,'owner':owner.strip(),
        'acquired_at':_iso(max(times)),'since':_iso(start),
        'target_dates':sorted({_day(v) for v in target_dates}),
        'observations':observations,'snapshots':sorted(snapshots,key=lambda v:v['as_of'])}
    data['sha256']=_digest(data)
    root=Path(base)/'data'/'acquisitions';folder=root/account
    if root.is_symlink() or folder.is_symlink():raise _error('取得証跡の保存先にリンクがあります。')
    folder.mkdir(parents=True,exist_ok=True)
    target=folder/(datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f_')+uuid.uuid4().hex[:12]+'.json')
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(dir=folder,prefix='.capture_',suffix='.tmp',delete=False) as stream:
            temporary=Path(stream.name);stream.write(_canonical(data));stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,target)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)
    return target


def _read(path,account,owner):
    try:
        if path.is_symlink() or path.stat().st_size>_MAX_BYTES:raise _error()
        data=json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError,TypeError):raise _error() from None
    if not isinstance(data,dict) or set(data)!=_TOP or data['schema']!='sbi-family-acquisition' or type(data['version']) is not int or data['version']!=1:raise _error()
    try:valid=isinstance(data['sha256'],str) and hmac.compare_digest(_digest(data),data['sha256'])
    except (ValueError,TypeError,OverflowError):valid=False
    if not valid:raise _error('取得証跡のチェックサムが一致しません。')
    if data['account']!=account or _owner(data['owner'])!=_owner(owner):raise _error('取得証跡と口座設定の名義が一致しません。')
    start,end=_stamp(data['since']),_stamp(data['acquired_at'])
    if end<start:raise _error()
    if not isinstance(data['target_dates'],list):raise _error()
    for value in data['target_dates']:_day(value)
    for key in ('observations','snapshots'):
        if not isinstance(data[key],list) or len(data[key])>10000:raise _error()
    observations=[_observation(item,True) for item in data['observations']]
    snapshots=[_snapshot(item,True) for item in data['snapshots']]
    for stamp in [_stamp(v['observed_at']) for v in observations]+[_stamp(v['payload']['recorded_at']) for v in snapshots]:
        if not start<=stamp<=end:raise _error()
    return {'account':account,'observations':observations,'snapshots':snapshots,
            'capture_window':{'account':account,'since':_iso(data['since']),'acquired_at':_iso(data['acquired_at'])}}


def _merge_snapshot(old,new,as_of):
    if old is None:return new
    if not isinstance(old,dict):raise _error()
    if not str(old.get('source','')).startswith('SBI'):return None
    previous,incoming=exact_fields(old),exact_fields(new)
    if previous and not incoming:return None
    if incoming:
        for key in previous&incoming:
            if old.get(key)!=new.get(key):raise _error('同じ基準日の確認済み資産と取得証跡が一致しません。')
        if previous and incoming<=previous:return None
        merged={key:(old.get(key) if key in previous else new.get(key) if key in incoming else None) for key in _FIELDS}
        merged.update(source='SBI 指定日資産',recorded_at=_iso(max(_stamp(old['recorded_at']),_stamp(new['recorded_at']))),
            evidence={'method':'displayed-date','as_of':as_of,'fields':sorted(previous|incoming)})
        return merged
    old_time,new_time=_stamp(old.get('recorded_at')),_stamp(new['recorded_at'])
    if old_time>new_time:return None
    if old_time==new_time:
        if any(old.get(key)!=new.get(key) for key in _FIELDS):raise _error('同時刻の資産取得証跡が一致しません。')
        return None
    return new


def replay_captures(base,stage,accounts=None):
    """Validate every selected journal first, then replay atomically into stage.

    No journal is removed or marked consumed. Exact timestamps and dated rows
    make repeated replay idempotent, including after a cancelled application.
    """
    selected=tuple('ABCD') if accounts is None else tuple(accounts)
    if len(set(selected))!=len(selected) or any(a not in tuple('ABCDE') for a in selected):raise _error()
    root=Path(base)/'data'/'acquisitions';journals=[]
    result={'accounts':[],'observations':0,'snapshots':0,'warnings':[],'proposal':[],'windows':[]}
    if not root.exists():return result
    if root.is_symlink():raise _error('取得証跡の保存先にリンクがあります。')
    for account in selected:
        if account=='E':continue
        folder=root/account
        if folder.is_symlink():raise _error()
        for path in sorted(folder.glob('*.json')):
            owner=_settings(stage,account)
            journals.append(_read(path,account,owner))
    if not journals:return result
    result['accounts']=sorted({v['account'] for v in journals})
    result['windows']=[v['capture_window'] for v in journals]
    savepoint='acquisition_replay_'+uuid.uuid4().hex
    stage.db.execute('SAVEPOINT '+savepoint)
    try:
        stage.db.execute('CREATE TABLE IF NOT EXISTS account_observations(account TEXT,kind TEXT,observed_at TEXT,payload TEXT,PRIMARY KEY(account,kind,observed_at))')
        stage.db.execute('CREATE TABLE IF NOT EXISTS asset_snapshots(account TEXT NOT NULL REFERENCES accounts(id),as_of TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(account,as_of))')
        for journal in journals:
            account=journal['account']
            for item in sorted(journal['observations'],key=lambda v:_stamp(v['observed_at'])):
                existing=[]
                for row in stage.db.execute('SELECT observed_at,payload FROM account_observations WHERE account=? AND kind=?',(account,item['kind'])):
                    if _stamp(row['observed_at'])==_stamp(item['observed_at']):existing.append(row)
                if existing:
                    for row in existing:
                        try:previous=_observation({'kind':item['kind'],'observed_at':row['observed_at'],'payload':json.loads(row['payload'])})
                        except (ValueError,TypeError):raise _error() from None
                        if previous!=item:raise _error('同時刻の資産取得証跡が一致しません。')
                    continue
                stage.db.execute('INSERT INTO account_observations VALUES (?,?,?,?)',
                    (account,item['kind'],item['observed_at'],json.dumps(item['payload'],ensure_ascii=False,allow_nan=False)))
                result['observations']+=1
                result['proposal'].append({'account':account,'kind':'資産取得証跡','state':'取得済みの表示値を復元','name':item['kind']})
            for item in sorted(journal['snapshots'],key=lambda v:(_stamp(v['payload']['recorded_at']),v['as_of'])):
                row=stage.db.execute('SELECT payload FROM asset_snapshots WHERE account=? AND as_of=?',(account,item['as_of'])).fetchone()
                try:old=json.loads(row['payload']) if row else None
                except (ValueError,TypeError):raise _error() from None
                merged=_merge_snapshot(old,item['payload'],item['as_of'])
                if merged is None:
                    if old and not str(old.get('source','')).startswith('SBI'):
                        warning='MANUAL_ASSET_PRESERVED:'+account+':'+item['as_of']
                        if warning not in result['warnings']:result['warnings'].append(warning)
                    continue
                merged=dict(merged,note='検証済みの資産取得証跡から復元。')
                stage.db.execute('INSERT OR REPLACE INTO asset_snapshots VALUES (?,?,?)',
                    (account,item['as_of'],json.dumps(merged,ensure_ascii=False,allow_nan=False)))
                result['snapshots']+=1
                result['proposal'].append({'account':account,'kind':'指定日資産','state':'取得済みの基準日を復元','name':item['as_of']})
        stage.db.execute('RELEASE '+savepoint)
    except BaseException:
        stage.db.execute('ROLLBACK TO '+savepoint);stage.db.execute('RELEASE '+savepoint)
        raise
    return result
