"""Validated local acquisition schedules; contains no credentials.

The built-in month-end/month-start entries are seeded once. Deletion is an
explicit tombstone so an upgrade or automatic registration cannot resurrect a
schedule the user has removed. Unknown/malformed settings are never replaced.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import unicodedata
from uuid import uuid4

JST = timezone(timedelta(hours=9), 'JST')
SCHEMA = 'sbi-acquisition-schedules'
MAX_BYTES = 512 * 1024
MAX_SCHEDULES = 500
_IDS = re.compile(r'[a-z][a-z0-9_-]{0,63}\Z')
_TIME = re.compile(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z')
_TOP = {'schema', 'version', 'revision', 'schedules'}
_ITEM = {'id', 'kind', 'label', 'time', 'run_at', 'enabled', 'deleted',
         'include_pdfs', 'revision'}


class ScheduleSettingsError(ValueError):
    pass


def _error(message='予約設定の形式・整合性を確認できません。'):
    return ScheduleSettingsError(message + ' 既存の設定を保持しました。')


def _revision(value):
    if type(value) is not int or not 1 <= value <= 2147483647:
        raise _error('予約の版番号を確認できません。')
    return value


def _aware(value):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError()
        return result.astimezone(JST)
    except (TypeError, ValueError, OverflowError):
        raise _error('予約日時にはタイムゾーン付きの日時が必要です。') from None


def _entry(value):
    if not isinstance(value, dict) or set(value) != _ITEM:
        raise _error()
    identifier = value['id']
    if not isinstance(identifier, str) or not _IDS.fullmatch(identifier):
        raise _error('予約IDを確認できません。')
    kind = value['kind']
    if kind not in ('month-end', 'month-start', 'once'):
        raise _error('予約の種類を確認できません。')
    if identifier in ('month-end', 'month-start'):
        if kind != identifier:
            raise _error('標準予約の種類は変更できません。')
    elif kind != 'once':
        raise _error('追加予約は指定日時の1回実行です。')
    label = value['label']
    if not isinstance(label, str) or not label.strip() or len(label) > 80 or any(
            unicodedata.category(c) in ('Cc', 'Cf') for c in label):
        raise _error('予約名は制御文字を含まない1～80文字にしてください。')
    moment = value['time']
    if not isinstance(moment, str) or not _TIME.fullmatch(moment):
        raise _error('時刻をHH:MM（00:00～23:59）で入力してください。')
    run_at = value['run_at']
    if kind == 'once':
        stamp = _aware(run_at)
        if stamp.second or stamp.microsecond or stamp.strftime('%H:%M') != moment:
            raise _error('指定日時と予約時刻は分単位で一致させてください。')
        run_at = stamp.isoformat()
    elif run_at is not None:
        raise _error('月末・月初の予約に単発日時は指定できません。')
    for key in ('enabled', 'deleted', 'include_pdfs'):
        if type(value[key]) is not bool:
            raise _error('予約の有効・削除・PDF設定を確認できません。')
    if value['deleted'] and value['enabled']:
        raise _error('削除した予約は有効にできません。明示的に復元してください。')
    return dict(id=identifier, kind=kind, label=label.strip(), time=moment,
                run_at=run_at, enabled=value['enabled'], deleted=value['deleted'],
                include_pdfs=value['include_pdfs'], revision=_revision(value['revision']))


def defaults():
    return dict(schema=SCHEMA, version=1, revision=1, schedules=[
        dict(id='month-end', kind='month-end', label='月末の資産取得', time='16:00',
             run_at=None, enabled=True, deleted=False, include_pdfs=False, revision=1),
        dict(id='month-start', kind='month-start', label='1日の資産・PDF取得', time='16:00',
             run_at=None, enabled=True, deleted=False, include_pdfs=True, revision=1)])


def validate_settings(value):
    if (not isinstance(value, dict) or set(value) != _TOP or value['schema'] != SCHEMA
            or type(value['version']) is not int or value['version'] != 1):
        raise _error()
    schedules = value['schedules']
    if not isinstance(schedules, list) or not 2 <= len(schedules) <= MAX_SCHEDULES:
        raise _error('予約件数を確認できません。')
    entries = [_entry(item) for item in schedules]
    identifiers = [item['id'] for item in entries]
    if len(identifiers) != len(set(identifiers)) or not {'month-end', 'month-start'} <= set(identifiers):
        raise _error('標準予約または予約IDの一意性を確認できません。')
    return dict(schema=SCHEMA, version=1, revision=_revision(value['revision']), schedules=entries)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def settings_digest(value):
    return hashlib.sha256(_canonical(validate_settings(value))).hexdigest()


def _link(path):
    return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) &
                                    getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0))


def _folder(base, create=False):
    root = Path(base).absolute()
    if not root.is_dir() or _link(root):
        raise _error('通常のアプリフォルダーを指定してください。')
    for part in ('data', 'scheduler'):
        root = root / part
        if root.exists() or root.is_symlink():
            if _link(root) or not root.is_dir():
                raise _error('予約設定の保存先にリンクは使用できません。')
        elif create:
            root.mkdir(exist_ok=True)
            if _link(root) or not root.is_dir():
                raise _error('予約設定の保存先を作成できません。')
    return root


def settings_path(base):
    return _folder(base) / 'acquisition_schedules.json'


def _regular(path):
    info = path.lstat()
    if _link(path) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
        raise _error('予約設定ファイルを安全に読み取れません。')
    return info


def _read(path):
    before = _regular(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise _error('読み取り中に予約設定が変わりました。')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            raw = stream.read(MAX_BYTES + 1)
    finally:
        os.close(descriptor)
    after = _regular(path)
    if len(raw) > MAX_BYTES or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise _error('読み取り中に予約設定が変わりました。')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise _error('予約設定の項目が重複しています。')
            result[key] = value
        return result
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=unique)
    except (UnicodeError, ValueError, RecursionError):
        raise _error('予約設定JSONを読み取れません。') from None


def _write(path, value):
    if path.exists() or path.is_symlink():
        _regular(path)
    descriptor, temporary = tempfile.mkstemp(prefix='.schedule_', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(_canonical(value)); stream.flush(); os.fsync(stream.fileno())
        if path.exists() or path.is_symlink():
            _regular(path)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _lock(folder):
    path = folder / '.schedule_settings.lock'
    if path.exists() or path.is_symlink():
        _regular(path)
    with path.open('a+b') as stream:
        stream.seek(0)
        if not stream.read(1):
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise _error('別の予約設定操作が実行中です。') from None
        yield


def _load(folder):
    path = folder / 'acquisition_schedules.json'
    if not path.exists() and not path.is_symlink():
        return defaults()
    return validate_settings(_read(path))


def load_settings(base, create=False):
    """Return defaults without writes unless explicitly creating the first file."""
    folder = _folder(base, create=create)
    if not create:
        return _load(folder)
    with _lock(folder):
        value = _load(folder)
        path = folder / 'acquisition_schedules.json'
        if not path.exists():
            _write(path, value)
        return value


def get_schedule(base, identifier):
    for entry in load_settings(base)['schedules']:
        if entry['id'] == identifier:
            return entry
    raise _error('指定された予約が見つかりません。')


def save_schedule(base, item, expected_revision=None):
    """Add a once entry or edit an existing entry; compare its own revision."""
    if not isinstance(item, dict) or set(item) - _ITEM:
        raise _error()
    folder = _folder(base, create=True)
    with _lock(folder):
        value = _load(folder)
        identifier = item.get('id') or 'once_' + uuid4().hex
        old = next((entry for entry in value['schedules'] if entry['id'] == identifier), None)
        if expected_revision is not None and expected_revision != (old['revision'] if old else 0):
            raise _error('ほかの操作で予約が変更されています。表示を更新してください。')
        entry = dict(old or dict(id=identifier, kind='once', label='指定日時の取得', time='16:00',
                     run_at=None, enabled=True, deleted=False, include_pdfs=True, revision=1))
        entry.update(item)
        entry['id'] = identifier
        entry['revision'] = old['revision'] + 1 if old else 1
        if entry['kind'] == 'once' and 'time' not in item and entry.get('run_at'):
            entry['time'] = _aware(entry['run_at']).strftime('%H:%M')
        entry = _entry(entry)
        if old:
            value['schedules'] = [entry if current['id'] == identifier else current for current in value['schedules']]
        else:
            value['schedules'].append(entry)
        value['revision'] += 1
        _write(folder / 'acquisition_schedules.json', validate_settings(value))
        return entry


def delete_schedule(base, identifier):
    current = get_schedule(base, identifier)
    return save_schedule(base, dict(current, enabled=False, deleted=True), expected_revision=current['revision'])


def restore_defaults(base):
    """Explicit restore preserves additional one-time reservations."""
    folder = _folder(base, create=True)
    with _lock(folder):
        value = _load(folder)
        builtin = {entry['id']: entry for entry in defaults()['schedules']}
        for old in value['schedules']:
            if old['id'] in builtin:
                builtin[old['id']]['revision'] = old['revision'] + 1
        value['schedules'] = [builtin.get(old['id'], old) for old in value['schedules']]
        value['revision'] += 1
        _write(folder / 'acquisition_schedules.json', validate_settings(value))
        return value


def read_registration_status(base):
    """Display only known registration fields; do not expose raw COM errors."""
    path = _folder(base) / 'task_registration.json'
    if not path.exists() and not path.is_symlink():
        return None
    value = _read(path)
    if not isinstance(value, dict):
        raise _error('Windows反映状況を確認できません。')
    status = value.get('status')
    if status not in ('registered', 'failed', 'needs-registration'):
        raise _error('Windows反映状況を確認できません。')
    mode = value.get('logon_mode')
    if mode not in ('InteractiveToken', 'Password'):
        raise _error('Windows反映のログオン方式を確認できません。')
    return dict(status=status, settings_revision=_revision(value.get('settings_revision')),
                logon_mode=mode, updated_at=_aware(value.get('updated_at')).isoformat(),
                message={'registered': '保存した予約をWindowsへ反映済みです。',
                         'failed': 'Windowsへの反映が未完了です。登録状態を確認してください。',
                         'needs-registration': 'パスワード方式の再登録操作が必要です。'}[status])
