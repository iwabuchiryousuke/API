"""Retire the former automatic schedules without removing financial history.

The local intent is written before any native work. Windows cleanup is limited
to this installation and a previous installation named by a validated receipt
for the current Windows identity. A native cleanup failure never means that an
older installation's jobs were successfully stopped.
"""
from datetime import datetime
import json
import os
from pathlib import Path, PureWindowsPath
import re
import subprocess
import sys

from schedule_settings import (JST, ScheduleSettingsError, _folder, _read, _write,
                               delete_schedule, load_settings)


_SCHEMA='sbi-automatic-schedules-retired'
_FIELDS={'schema','version','retired','requested_at','app_roots','native_status',
         'preferences_status','error_code','identity_key'}
_STATUSES={'pending','not-windows','started','removed','failed'}
_PREFERENCES={'disabled','unreadable'}
_ERRORS={None,'SETTINGS_UNREADABLE','PROCESS_START_FAILED','NATIVE_REMOVAL_FAILED',
         'REGISTRATION_BUSY','RETIREMENT_RECORD_UNREADABLE','IDENTITY_UNAVAILABLE'}


class ScheduleRetirementError(ScheduleSettingsError):
    pass


def _error():
    return ScheduleRetirementError('旧自動取得の停止記録を確認できません。元の記録を保持し、予約の再登録を停止しました。')


def _root_text(value):
    if not isinstance(value,str) or not value or len(value)>2048 or any(ord(c)<32 for c in value):raise _error()
    if not (Path(value).is_absolute() or PureWindowsPath(value).is_absolute()):raise _error()
    return value


def _path(base,create=False):
    return _folder(base,create=create)/'automatic_schedules_retired.json'


def read_retirement(base):
    path=_path(base)
    if not path.exists() and not path.is_symlink():return None
    try:
        value=_read(path)
        if (not isinstance(value,dict) or set(value)!=_FIELDS or value['schema']!=_SCHEMA
                or type(value['version']) is not int or value['version']!=1 or value['retired'] is not True
                or value['native_status'] not in _STATUSES or value['preferences_status'] not in _PREFERENCES
                or value['error_code'] not in _ERRORS):raise _error()
        roots=value['app_roots']
        if not isinstance(roots,list) or not 1<=len(roots)<=16 or len(set(roots))!=len(roots):raise _error()
        for root in roots:_root_text(root)
        identity=value['identity_key']
        if identity is not None and (not isinstance(identity,str) or not re.fullmatch(r'[a-f0-9]{64}',identity)):raise _error()
        stamp=datetime.fromisoformat(value['requested_at'])
        if stamp.tzinfo is None or stamp.utcoffset() is None:raise _error()
        return value
    except (OSError,ValueError,TypeError,KeyError,OverflowError):raise _error() from None


def is_retired(base):
    """Missing is false; corrupt records fail closed instead of re-enabling jobs."""
    return read_retirement(base) is not None


def _write_state(base,value,**updates):
    current=dict(value,**updates)
    _write(_path(base,create=True),current)
    return current


def mark_native_result(base,status,error_code=None):
    if status not in _STATUSES or error_code not in _ERRORS:raise _error()
    value=read_retirement(base)
    if value is None:raise _error()
    return _write_state(base,value,native_status=status,error_code=error_code)


def prepare_retirement(base):
    """Persist stop intent first, then tombstone saved choices without erasure."""
    base=Path(base).resolve()
    value=read_retirement(base)
    roots=[str(base)]
    identity_error=False
    identity=None
    # A copied data folder can still point at the old application's tasks.
    # Do not read or write anything under the old root; only its task receipt.
    try:
        from windows_tasks import _identity_key,_registration_state
        identity=_identity_key()
        if value and value['identity_key']==identity:
            roots=list(value['app_roots'])
            if str(base) not in roots:roots.append(str(base))
        receipt=_registration_state(base)
        if receipt and receipt['identity_key']==identity:
            old=_root_text(receipt['app_root'])
            if old not in roots:roots.append(old)
        elif receipt:identity_error=True
    except (OSError,ValueError,RuntimeError):
        identity_error=True
    if len(roots)>16:raise _error()
    value=dict(schema=_SCHEMA,version=1,retired=True,
               requested_at=value['requested_at'] if value else datetime.now(JST).isoformat(),
               app_roots=roots,native_status='pending',preferences_status='disabled',
               error_code='IDENTITY_UNAVAILABLE' if identity_error else None,identity_key=identity)
    _write_state(base,value)  # This is the authoritative gate, before settings/native changes.
    try:
        settings=load_settings(base,create=True)
        for entry in settings['schedules']:
            if not entry['deleted']:delete_schedule(base,entry['id'])
    except (OSError,ValueError):
        value=_write_state(base,value,preferences_status='unreadable',error_code='SETTINGS_UNREADABLE')
    return value


def retire_legacy_schedules(base,popen=subprocess.Popen,platform_name=None):
    """App startup hook. Native cleanup runs separately; financial DB stays opaque."""
    base=Path(base).resolve()
    try:
        value=prepare_retirement(base)
    except (OSError,ValueError):
        return dict(status='failed',process=None,error_code='RETIREMENT_RECORD_UNREADABLE')
    try:
        if (platform_name or os.name)!='nt':
            mark_native_result(base,'not-windows',value['error_code'])
            return dict(status='disabled-locally',process=None,error_code=value['error_code'])
        mark_native_result(base,'started',value['error_code'])
        process=popen([sys.executable,str(base/'windows_tasks.py'),'--action','retire'],
                      cwd=str(base),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        return dict(status='native-cleanup-started',process=process,error_code=value['error_code'])
    except (OSError,ValueError):
        try:mark_native_result(base,'failed','PROCESS_START_FAILED')
        except (OSError,ValueError):pass
        return dict(status='failed',process=None,error_code='PROCESS_START_FAILED')
