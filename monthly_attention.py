"""Owner-bound monthly authentication holds; no raw errors or credentials.

A scheduled retry can read these markers without another authentication
attempt. Only a deliberate successful test may call clear_attention.
Checksums detect corruption and do not authenticate externally edited files.
"""
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import unicodedata

from ledger import LedgerError
from login_credentials import CredentialError


JST=timezone(timedelta(hours=9),'JST')
MANUAL_CODES=frozenset({
    'MANUAL_AUTH_REQUIRED','NEEDS_CONSENT','SCOPE_MISSING','OFFLINE_PERMISSION',
    'CLIENT_MISSING','CLIENT_CHANGED','CLIENT_INVALID','CLIENT_CONFIG',
    'CLIENT_JSON','CLIENT_AMBIGUOUS','RECIPIENT_MISSING','RECIPIENT_INVALID',
    'RECIPIENT_CONFIG','RECIPIENT_MISMATCH','MAILBOX_MISSING','MAILBOX_INVALID',
    'MAILBOX_CONFIG','MAILBOX_MISMATCH','ACCOUNT_CONFIG','LOCAL_SETTINGS',
    'ACCESS_DENIED','API_FORBIDDEN','DEPENDENCY',
})
_SCHEMA='sbi-monthly-attention'
_TOP={'schema','version','account','owner','slot','code','recorded_at','sha256'}
_MAX_BYTES=8192


def _error():
    return LedgerError('月次取得の認証待ち記録の名義・日時・整合性を確認できません。既存記録を保持して停止しました。')


def attention_required(error):
    """Recognize positive manual/configuration failures, never message text."""
    if isinstance(error,CredentialError) and getattr(error,'retryable',None) is False:return True
    from gmail_auth import GmailError
    return isinstance(error,GmailError) and isinstance(error.code,str) and error.code in MANUAL_CODES


def _account(value):
    if value not in tuple('ABCD'):raise _error()
    return value


def _owner(value):
    if not isinstance(value,str) or not value.strip() or len(value)>50:raise _error()
    normalized=''.join(c for c in unicodedata.normalize('NFKC',value) if not c.isspace() and unicodedata.category(c)!='Cf')
    if not normalized or any(unicodedata.category(c)=='Cc' for c in normalized):raise _error()
    return normalized


def _slot(value):
    from monthly_records import _slot as validate
    try:return validate(value,strict=True)
    except (ValueError,TypeError,KeyError,OverflowError):raise _error() from None


def _canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
def _digest(value):return hashlib.sha256(_canonical({k:v for k,v in value.items() if k!='sha256'})).hexdigest()


def _link(path):
    junction=getattr(path,'is_junction',None)
    return path.is_symlink() or bool(junction and junction()) or bool(getattr(path.lstat(),'st_file_attributes',0)&getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0))


def _directory(path,create=False):
    if path.exists() or path.is_symlink():
        if _link(path) or not path.is_dir():raise _error()
    elif create:
        path.mkdir(exist_ok=True)
        if _link(path) or not path.is_dir():raise _error()
    return path


def _root(base,create=False):
    current=Path(base).absolute()
    if not current.is_dir() or _link(current):raise _error()
    current=current.resolve()
    for name in ('data','scheduler','attention'):current=_directory(current/name,create)
    return current


def _safe_file(path):
    info=path.lstat()
    if _link(path) or not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>_MAX_BYTES:raise _error()
    return info


def _read(path,account,owner,slot=None):
    try:
        before=_safe_file(path)
        descriptor=os.open(path,os.O_RDONLY|getattr(os,'O_BINARY',0)|getattr(os,'O_NOFOLLOW',0))
        try:
            opened=os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink!=1 or (opened.st_dev,opened.st_ino)!=(before.st_dev,before.st_ino):raise _error()
            with os.fdopen(descriptor,'rb',closefd=False) as handle:raw=handle.read(_MAX_BYTES+1)
        finally:os.close(descriptor)
        after=_safe_file(path)
        if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns) or len(raw)>_MAX_BYTES:raise _error()
        def unique(pairs):
            out={}
            for key,value in pairs:
                if key in out:raise _error()
                out[key]=value
            return out
        value=json.loads(raw.decode('utf-8'),object_pairs_hook=unique)
        if not isinstance(value,dict) or set(value)!=_TOP or value['schema']!=_SCHEMA or type(value['version']) is not int or value['version']!=1:raise _error()
        checksum=value['sha256']
        if not isinstance(checksum,str) or not re.fullmatch(r'[a-f0-9]{64}',checksum) or not hmac.compare_digest(checksum,_digest(value)):raise _error()
        if value['account']!=_account(account) or value['owner']!=_owner(owner) or value['code'] not in MANUAL_CODES:raise _error()
        normalized=_slot(value['slot'])
        if normalized!=value['slot'] or normalized['slot_date']!=path.parent.name or path.name!=account+'.json':raise _error()
        if slot is not None and normalized!=slot:raise _error()
        stamp=datetime.fromisoformat(value['recorded_at'])
        if stamp.tzinfo is None or stamp.utcoffset() is None or stamp.astimezone(JST).isoformat()!=value['recorded_at']:raise _error()
        return value
    except LedgerError:raise
    except (OSError,ValueError,TypeError,KeyError,OverflowError,RecursionError):raise _error() from None


@contextmanager
def _lock(root):
    path=root/'.attention.lock'
    if path.exists() or path.is_symlink():_safe_file(path)
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
        except OSError:raise LedgerError('別の認証待ち記録の更新が実行中です。') from None
        try:yield
        finally:
            handle.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(),fcntl.LOCK_UN)


def save_attention(base,slot,account,owner,code='MANUAL_AUTH_REQUIRED'):
    """Persist fixed metadata only after a positively manual failure."""
    account=_account(account);owner=_owner(owner);slot=_slot(slot)
    if not isinstance(code,str) or code not in MANUAL_CODES:raise _error()
    root=_root(base,True);temporary=None
    try:
        with _lock(root):
            folder=_directory(root/slot['slot_date'],True);path=folder/(account+'.json')
            if path.exists() or path.is_symlink():_read(path,account,owner,slot);return path
            value={'schema':_SCHEMA,'version':1,'account':account,'owner':owner,'slot':slot,
                   'code':code,'recorded_at':datetime.now(JST).isoformat()}
            value['sha256']=_digest(value)
            with tempfile.NamedTemporaryFile(prefix='.attention_',suffix='.tmp',dir=folder,delete=False) as handle:
                temporary=Path(handle.name);handle.write(_canonical(value));handle.flush();os.fsync(handle.fileno())
            if path.exists() or path.is_symlink():raise _error()
            os.replace(temporary,path)
            if _read(path,account,owner,slot)!=value:raise _error()
            return path
    except LedgerError:raise
    except (OSError,ValueError,TypeError):raise _error() from None
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)


def blocked(base,slot,account,owner):
    """Read-only check for this owner and this exact planned role/date."""
    account=_account(account);owner=_owner(owner);slot=_slot(slot)
    root=_root(base);folder=_directory(root/slot['slot_date']);path=folder/(account+'.json')
    if not path.exists() and not path.is_symlink():return False
    _read(path,account,owner,slot);return True


def clear_attention(base,account,owner):
    """Call only after an explicit successful manual test of this owner.

    Validate every matching marker before any deletion. A mismatched owner or
    corrupt marker is retained and stops clearing, rather than silently removed.
    Returns the number of cleared markers; it never edits credential files.
    """
    account=_account(account);owner=_owner(owner);root=_root(base)
    if not root.exists():return 0
    try:
        with _lock(root):
            paths=[]
            for folder in sorted(root.iterdir()):
                if folder.name=='.attention.lock':continue
                if _link(folder):raise _error()
                if not folder.is_dir() or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',folder.name):continue
                path=folder/(account+'.json')
                if path.exists() or path.is_symlink():_read(path,account,owner);paths.append(path)
                if len(paths)>2000:raise _error()
            for path in paths:
                _read(path,account,owner);path.unlink()
            return len(paths)
    except LedgerError:raise
    except (OSError,ValueError,TypeError):raise _error() from None
