"""Immutable, account-bound monthly SBI summary observations.

These are actual observation times around a planned 16:00 JST collection, not
proof of an exact 16:00 valuation. Checksums detect corruption, not forgery.
The transaction ledger and existing asset schemas are never opened or changed.
"""
from contextlib import contextmanager
from datetime import date,datetime,time,timedelta,timezone
from decimal import Decimal,localcontext
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import unicodedata
from uuid import uuid4


JST=timezone(timedelta(hours=9),'JST')
SOURCE='SBI口座サマリー円建の株式表示額'
REQUIRED=('summary_stocks','cash_total')
AMOUNTS=('summary_stocks','summary_total','cash_total','broker_cash','bank')
NISA=('growth_remaining','tsumitate_remaining')
MAX_RECORD_BYTES=64*1024
MAX_RECORDS=20000
_SCHEMA='sbi-monthly-summary'
_TOP={'schema','version','account','owner','attempt_id','slot','summary','timing',
      'delay_seconds','source','displayed_at_is_valuation','sha256'}
_SLOT={'role','scheduled_for','slot_date','comparison_month','include_pdfs'}
_SUMMARY={'complete','missing','values','observed_at','displayed_at'}
_ATTEMPT=re.compile(r'[A-Za-z0-9_-]{1,80}\Z')
_HASH=re.compile(r'[a-f0-9]{64}\Z')


class MonthlyRecordError(ValueError):
    """An observation could not be saved or validated without guessing."""


def _error(message='月次記録の形式・整合性を確認できません。'):
    return MonthlyRecordError(message+' 既存の記録と取引台帳は変更していません。')


def _account(value):
    if value not in tuple('ABCD'):raise _error('月次自動取得の対象口座はA～Dです。')
    return value


def _owner(value):
    if not isinstance(value,str) or not value.strip() or len(value)>50:raise _error('口座名義を確認できません。')
    result=''.join(c for c in unicodedata.normalize('NFKC',value) if not c.isspace() and unicodedata.category(c)!='Cf')
    if not result or any(unicodedata.category(c)=='Cc' for c in result):raise _error('口座名義を確認できません。')
    return result


def _month(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d{4}-(?:0[1-9]|1[0-2])',value):raise _error('比較対象月はYYYY-MMで指定してください。')
    try:
        day=date.fromisoformat(value+'-01')
        if day.year<1900:raise ValueError()
        return day
    except ValueError:raise _error('比較対象月を確認できません。') from None


def _day(value):
    try:
        if not isinstance(value,str) or date.fromisoformat(value).isoformat()!=value:raise ValueError()
        return date.fromisoformat(value)
    except (TypeError,ValueError):raise _error('予定日をYYYY-MM-DDで指定してください。') from None


def _stamp(value):
    try:
        if not isinstance(value,str) or len(value)>64:raise ValueError()
        result=datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() is None:raise ValueError()
        return result.astimezone(JST)
    except (TypeError,ValueError,OverflowError):raise _error('タイムゾーンを含む取得時刻・予定時刻が必要です。') from None


def _number(value):
    if value is None:return None
    if isinstance(value,bool) or not isinstance(value,(int,float,Decimal)):raise _error('表示額は0以上の有限数値か未取得で指定してください。')
    try:
        result=float(value)
        if not math.isfinite(result) or result<0:raise ValueError()
    except (TypeError,ValueError,OverflowError):raise _error('表示額は0以上の有限数値が必要です。') from None
    if isinstance(value,Decimal) and Decimal(str(result))!=value:raise _error('表示額の精度を数値形式で保持できません。')
    return value if type(value) is int else result


def _decimal_sum(values):
    # Cover the full finite float range plus integer yen without losing a unit
    # when a large account balance and a small component are compared.
    with localcontext() as context:
        context.prec=700
        return sum((Decimal(str(value)) for value in values),Decimal(0))


def _decimal_difference(before,after):
    with localcontext() as context:
        context.prec=700
        return Decimal(str(after))-Decimal(str(before))


def _delta(before,after):return float(_decimal_difference(before,after))


def _page_clock(value):
    if value is None:return None
    if not isinstance(value,str) or len(value)>64 or '\n' in value or '\r' in value:raise _error('画面の表示時計を確認できません。')
    for fmt in ('%Y/%m/%d %H:%M','%Y/%m/%d %H:%M:%S'):
        try:datetime.strptime(value,fmt);return value
        except ValueError:pass
    try:datetime.fromisoformat(value);return value
    except ValueError:raise _error('画面の表示時計を確認できません。') from None


def _slot(value,strict=False):
    if not isinstance(value,dict) or not (_SLOT-{'include_pdfs'})<=value.keys():raise _error('月次取得の予定情報が不足しています。')
    if strict and set(value)!=_SLOT:raise _error('保存された予定情報に不明な項目があります。')
    first=_month(value['comparison_month']);day=_day(value['slot_date'])
    role=value['role']
    expected=first-timedelta(days=1) if role=='month-end' else first if role=='month-start' else None
    planned=_stamp(value['scheduled_for'])
    include=value.get('include_pdfs',False)
    if day!=expected or planned.date()!=day or planned.second or planned.microsecond:raise _error('月末・月初の日付と分単位の予定時刻が一致しません。')
    if not isinstance(include,bool):raise _error('PDF取得の予定設定を確認できません。')
    return dict(role=role,scheduled_for=planned.isoformat(),slot_date=day.isoformat(),
                comparison_month=first.strftime('%Y-%m'),include_pdfs=include)


def _summary(value,strict=False):
    if not isinstance(value,dict) or set(value)!=_SUMMARY:raise _error('口座サマリーの取得結果を確認できません。')
    values=value['values']
    if not isinstance(values,dict) or set(values)-set(AMOUNTS+NISA):raise _error('口座サマリーに不明な金額項目があります。')
    out={key:_number(values.get(key)) for key in AMOUNTS if key in values or key in REQUIRED}
    for key in NISA:
        if key not in values:continue
        item=values[key]
        if item is None:out[key]=None;continue
        if not isinstance(item,dict) or set(item)!={'year','remaining'} or type(item['year']) is not int or not 1900<=item['year']<=9999:raise _error('NISA枠の対象年・残額を確認できません。')
        out[key]={'year':item['year'],'remaining':_number(item['remaining'])}
    missing=[key for key in REQUIRED if out.get(key) is None]
    supplied=value['missing']
    if not isinstance(supplied,list) or any(not isinstance(key,str) for key in supplied) or len(set(supplied))!=len(supplied) or set(supplied)!=set(missing):raise _error('取得済み・未取得項目の判定が表示額と一致しません。')
    if type(value['complete']) is not bool or value['complete']!=(not missing):raise _error('取得完了の判定が株式・現金の読取結果と一致しません。')
    total=out.get('summary_total');known=[out[key] for key in REQUIRED if out[key] is not None]
    if total is not None and known and _decimal_sum((total,Decimal('.01')))<_decimal_sum(known):
        raise _error('口座表示の合計が読み取った株式・現金より小さく、整合しません。')
    if all(out.get(key) is not None for key in ('cash_total','broker_cash','bank')):
        parts=_decimal_sum((out['broker_cash'],out['bank']))
        if abs(_decimal_difference(parts,out['cash_total']))>Decimal('.01'):raise _error('現金合計と明示された証券・銀行残高が一致しません。')
    return dict(complete=not missing,missing=missing,values=out,
                observed_at=_stamp(value['observed_at']).isoformat(),displayed_at=_page_clock(value['displayed_at']))


def _canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
def _digest(value):return hashlib.sha256(_canonical({k:v for k,v in value.items() if k!='sha256'})).hexdigest()


def _record(account,owner,slot,summary,attempt_id):
    account=_account(account);owner=_owner(owner);slot=_slot(slot);summary=_summary(summary)
    if not isinstance(attempt_id,str) or not _ATTEMPT.fullmatch(attempt_id):raise _error('取得試行の識別子を確認できません。')
    observed=_stamp(summary['observed_at']);planned=_stamp(slot['scheduled_for'])
    delay=(observed-planned).total_seconds()
    if observed.date().isoformat()!=slot['slot_date']:raise _error('別の日の取得値を予定日の記録として保存できません。')
    if delay<0:raise _error('予定時刻より前の取得値を完了記録として保存できません。')
    data={'schema':_SCHEMA,'version':1,'account':account,'owner':owner,'attempt_id':attempt_id,
          'slot':slot,'summary':summary,'timing':'near-schedule' if delay<=1800 else 'late',
          'delay_seconds':delay,'source':SOURCE,'displayed_at_is_valuation':False}
    data['sha256']=_digest(data)
    return data


def _is_link(path):
    junction=getattr(path,'is_junction',None)
    return path.is_symlink() or bool(junction and junction()) or bool(
        getattr(path.lstat(),'st_file_attributes',0)&getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0))


def _root(base,create=False):
    base=Path(base).absolute()
    if not base.is_dir() or _is_link(base):raise _error('アプリの通常フォルダーを指定してください。')
    current=base.resolve()
    for part in ('data','monthly','measurements'):
        current=current/part
        if current.exists() or current.is_symlink():
            if _is_link(current) or not current.is_dir():raise _error('月次記録の保存先にリンク・不正なフォルダーがあります。')
        elif create:
            current.mkdir(exist_ok=True)
            if _is_link(current) or not current.is_dir():raise _error('月次記録の保存先を安全に作成できません。')
    return current


def _folder(root,account,create=False):
    path=root/_account(account)
    if path.exists() or path.is_symlink():
        if _is_link(path) or not path.is_dir():raise _error('口座別の保存先にリンク・不正なフォルダーがあります。')
    elif create:
        path.mkdir(exist_ok=True)
        if _is_link(path) or not path.is_dir():raise _error('口座別の保存先を安全に作成できません。')
    return path


def _filename(data):return data['slot']['slot_date']+'_'+data['slot']['role']+'_'+data['attempt_id']+'.json'


def _parse(raw):
    def unique(pairs):
        value={}
        for key,item in pairs:
            if key in value:raise _error('月次記録の項目が重複しています。')
            value[key]=item
        return value
    try:return json.loads(raw.decode('utf-8'),object_pairs_hook=unique)
    except (UnicodeError,ValueError,RecursionError) as error:
        if isinstance(error,MonthlyRecordError):raise
        raise _error('月次記録を読み取れません。') from None


def _read(path,account,owner=None):
    try:
        info=path.lstat()
        if _is_link(path) or not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>MAX_RECORD_BYTES:raise _error('月次記録のファイルを安全に読み取れません。')
        descriptor=os.open(path,os.O_RDONLY|getattr(os,'O_BINARY',0)|getattr(os,'O_NOFOLLOW',0))
        try:
            opened=os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink!=1 or (opened.st_dev,opened.st_ino)!=(info.st_dev,info.st_ino):raise _error('月次記録が読み取り中に変更されました。')
            with os.fdopen(descriptor,'rb',closefd=False) as handle:raw=handle.read(MAX_RECORD_BYTES+1)
        finally:os.close(descriptor)
        after=path.lstat()
        if _is_link(path) or (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns)!=(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns):raise _error('月次記録が読み取り中に変更されました。')
        if len(raw)>MAX_RECORD_BYTES:raise _error('月次記録がサイズ上限を超えています。')
    except OSError:raise _error('月次記録のファイルへアクセスできません。') from None
    value=_parse(raw)
    if not isinstance(value,dict) or set(value)!=_TOP or value.get('schema')!=_SCHEMA or type(value.get('version')) is not int or value['version']!=1:raise _error('保存された月次記録の形式が不正です。')
    checksum=value.get('sha256')
    try:valid=isinstance(checksum,str) and _HASH.fullmatch(checksum) and hmac.compare_digest(checksum,_digest(value))
    except (TypeError,ValueError,OverflowError,RecursionError):valid=False
    if not valid:raise _error('月次記録のチェックサムが一致しません。')
    if value['account']!=_account(account) or (owner is not None and _owner(value['owner'])!=_owner(owner)):raise _error('月次記録と選択口座の名義が一致しません。')
    _slot(value['slot'],strict=True)
    normalized=_record(value['account'],value['owner'],value['slot'],value['summary'],value['attempt_id'])
    if normalized!=value or _filename(value)!=path.name:raise _error('月次記録の内容・保存名が正規の形式と一致しません。')
    return value


@contextmanager
def _write_lock(root):
    path=root/'.records.lock'
    if path.exists() and (_is_link(path) or not path.is_file() or path.stat().st_nlink!=1):raise _error('月次記録の保存ロックが不正です。')
    if path.is_symlink():raise _error('月次記録の保存ロックにリンクは使用できません。')
    with path.open('a+b') as handle:
        handle.seek(0)
        if not handle.read(1):handle.write(b'0');handle.flush()
        handle.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:raise _error('別の月次記録保存が実行中です。') from None
        try:yield
        finally:
            handle.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(),fcntl.LOCK_UN)


def save_measurement(base,account,owner,slot,summary,attempt_id=None):
    """Save a new immutable attempt; identical retry IDs are idempotent only."""
    data=_record(account,owner,slot,summary,uuid4().hex if attempt_id is None else attempt_id)
    root=_root(base,create=True);folder=_folder(root,account,create=True)
    path=folder/_filename(data);temporary=None
    try:
        with _write_lock(root):
            if path.exists() or path.is_symlink():
                if _read(path,account,owner)==data:return path
                raise _error('同じ取得試行の既存記録は上書きできません。新しい試行として取得してください。')
            with tempfile.NamedTemporaryFile(dir=folder,prefix='.monthly_',suffix='.tmp',delete=False) as handle:
                temporary=Path(handle.name);handle.write(_canonical(data));handle.flush();os.fsync(handle.fileno())
            if path.exists() or path.is_symlink():raise _error('保存中に同じ取得試行の記録が作られました。上書きせず停止します。')
            os.replace(temporary,path)
            if _read(path,account,owner)!=data:raise _error('保存した月次記録の内容が一致しません。')
            try:
                fd=os.open(folder,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
                try:os.fsync(fd)
                finally:os.close(fd)
            except OSError:pass # Windows does not expose directory fsync.
        return path
    except OSError:raise _error('月次記録を安全に保存できません。保存先・空き容量を確認してください。') from None
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)


def _selection(accounts,owners):
    if owners is not None:
        if not isinstance(owners,dict):raise _error('口座ごとの名義設定が必要です。')
        owners={_account(account):_owner(owner) for account,owner in owners.items()}
    selected=tuple(accounts) if accounts is not None else tuple(owners) if owners is not None else tuple('ABCD')
    if len(set(selected))!=len(selected):raise _error('対象口座が重複しています。')
    for account in selected:
        _account(account)
        if owners is not None and account not in owners:raise _error('対象口座の名義設定が不足しています。')
    return selected,owners


def load_measurements(base,accounts=None,owners=None):
    """Validate every selected record before returning any financial values."""
    selected,owners=_selection(accounts,owners);root=_root(base);result=[]
    if not root.exists():return result
    for account in selected:
        folder=_folder(root,account)
        if not folder.exists():continue
        for path in sorted(folder.iterdir()):
            if _is_link(path):raise _error('月次記録にリンクは使用できません。')
            if path.name.startswith('.monthly_') and path.suffix=='.tmp':continue
            if path.suffix!='.json' or not path.is_file():raise _error('月次記録フォルダーに想定外のファイルがあります。')
            result.append(_read(path,account,owners.get(account) if owners is not None else None))
            if len(result)>MAX_RECORDS:raise _error('月次記録の件数が安全な読み取り上限を超えています。')
    return sorted(result,key=lambda value:(value['slot']['slot_date'],value['account'],value['summary']['observed_at'],value['attempt_id']))


def _slot_key(slot):return tuple(slot[key] for key in ('role','scheduled_for','slot_date','comparison_month'))


def _choose(records,account,slot):
    found=[value for value in records if value['account']==account and _slot_key(value['slot'])==_slot_key(slot)]
    if not found:return None
    return min(found,key=lambda value:(not value['summary']['complete'],len(value['summary']['missing']),
        value['timing']!='near-schedule',value['delay_seconds'],value['summary']['observed_at'],value['attempt_id']))


def select_measurement(base,account,slot,owner):
    """Return one coherent best attempt, or None; never mix retry fields."""
    account=_account(account);slot=_slot(slot);owner=_owner(owner)
    return _choose(load_measurements(base,accounts=(account,),owners={account:owner}),account,slot)


def _choose_calendar(records,account,slot):
    # An edited time creates a different occurrence. Within one occurrence,
    # keep its first complete attempt. Across occurrences, show the latest
    # coherent complete observation instead of combining its individual fields.
    found=[value for value in records if value['account']==account and
           all(value['slot'][key]==slot[key] for key in ('role','slot_date','comparison_month'))]
    groups={value['slot']['scheduled_for'] for value in found}
    candidates=[_choose(found,account,dict(slot,scheduled_for=planned)) for planned in groups]
    if not candidates:return None
    return min(candidates,key=lambda value:(not value['summary']['complete'],
        len(value['summary']['missing']),-(_stamp(value['summary']['observed_at']).timestamp()),
        value['delay_seconds'],value['attempt_id']))


def comparison(base,month,owners):
    """Compare the same owner's prior month-end and current month-start slots.

    Differences are account display changes, not investment profits. Missing
    fields never become zero and optional NISA deltas require the same year.
    """
    first=_month(month);selected,owners=_selection(None,owners)
    if owners is None:raise _error('比較には現在の口座名義設定が必要です。')
    records=load_measurements(base,accounts=selected,owners=owners);slots={}
    from schedule_settings import load_settings,ScheduleSettingsError
    try:configured={entry['kind']:entry['time'] for entry in load_settings(base)['schedules'] if entry['kind']!='once'}
    except (ScheduleSettingsError,OSError):raise _error('月次予約の現在の時刻設定を確認できません。') from None
    for role,day in (('month-end',first-timedelta(days=1)),('month-start',first)):
        hour,minute=map(int,configured.get(role,'16:00').split(':'))
        slots[role]=dict(role=role,scheduled_for=datetime.combine(day,time(hour,minute),JST).isoformat(),
            slot_date=day.isoformat(),comparison_month=first.strftime('%Y-%m'),include_pdfs=role=='month-start')
    result={'month':first.strftime('%Y-%m'),'source':SOURCE,
            'note':'差額は同じ口座の表示額の増減であり、運用損益ではありません。予定時刻の確定残高を示すものではありません。未取得側の予定時刻は現在の予約設定です。',
            'accounts':{}}
    for account in selected:
        before=_choose_calendar(records,account,slots['month-end']);after=_choose_calendar(records,account,slots['month-start'])
        bv=before['summary']['values'] if before else {};av=after['summary']['values'] if after else {}
        deltas={};missing=[]
        for key in AMOUNTS:
            left,right=bv.get(key),av.get(key)
            deltas[key]=_delta(left,right) if left is not None and right is not None else None
        for key in NISA:
            left,right=bv.get(key),av.get(key)
            same=bool(left is not None and right is not None and left.get('year')==right.get('year'))
            known=same and left.get('remaining') is not None and right.get('remaining') is not None
            deltas[key]=_delta(left['remaining'],right['remaining']) if known else None
        for role,record in (('month-end',before),('month-start',after)):
            for key in REQUIRED:
                if record is None or record['summary']['values'].get(key) is None:missing.append(role+':'+key)
        complete=before is not None and after is not None and not missing
        status='missing' if before is None or after is None else 'partial' if missing else 'late' if any(record['timing']=='late' for record in (before,after)) else 'complete'
        selected_records={'month-end':before,'month-start':after}
        result['accounts'][account]={'account':account,'owner':owners[account],
            'month_end':before,'month_start':after,'deltas':deltas,'missing':missing,'status':status,'complete':complete,
            'timing':{role:record['timing'] if record else 'missing' for role,record in selected_records.items()},
            'observed_at':{role:record['summary']['observed_at'] if record else None for role,record in selected_records.items()},
            'scheduled_for':{role:record['slot']['scheduled_for'] if record else slots[role]['scheduled_for'] for role,record in selected_records.items()}}
    return result
