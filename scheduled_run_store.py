"""Immutable evidence for configured and one-time unattended acquisitions.

The plan hash binds an occurrence to its exact reservation revision and time.
Only actual same-day observations at or after that time can complete it. This
module never opens the trade database or reads credentials. Checksums detect
corruption; they are not signatures for externally edited data.
"""
from calendar import monthrange
from contextlib import contextmanager
from datetime import date, datetime, timedelta
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from uuid import uuid4

from ledger import LedgerError
from monthly_attention import MANUAL_CODES
from monthly_records import JST, SOURCE, _account, _owner, _stamp, _summary

PLAN_KEYS = {'schedule_id', 'schedule_revision', 'kind', 'scheduled_for',
             'slot_date', 'include_pdfs', 'comparison_month'}
_ID = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}\Z')
_HASH = re.compile(r'[a-f0-9]{64}\Z')
_ATTEMPT = re.compile(r'[a-f0-9]{32}\Z')
_MAX_BYTES = 65536
_MAX_RECORDS = 20000


class ScheduledRecordError(LedgerError):
    """Saved evidence cannot be safely accepted, rebound, or overwritten."""


def _error(message='予約取得記録の名義・日時・整合性を確認できません。'):
    return ScheduledRecordError(message + ' 既存の記録を保持して停止しました。')


def validate_plan(value):
    try:
        if not isinstance(value, dict) or set(value) != PLAN_KEYS:
            raise _error('予約取得の予定項目を確認できません。')
        identifier = value['schedule_id']
        revision = value['schedule_revision']
        if (not isinstance(identifier, str) or not _ID.fullmatch(identifier)
                or type(revision) is not int or not 1 <= revision <= 2147483647
                or type(value['include_pdfs']) is not bool):
            raise _error('予約の識別子・版・PDF設定を確認できません。')
        planned = _stamp(value['scheduled_for'])
        if planned.second or planned.microsecond or planned.year < 1900:
            raise _error('予約日時は日本時間の分単位で指定してください。')
        day = date.fromisoformat(value['slot_date'])
        if day.isoformat() != value['slot_date'] or day != planned.date():
            raise _error('予約日と予定日時が一致しません。')
        kind = value['kind']
        if kind == 'once':
            if value['comparison_month'] is not None:
                raise _error('単発予約を月末・月初の比較記録へ読み替えられません。')
        elif kind in ('month-end', 'month-start'):
            expected_day = monthrange(day.year, day.month)[1] if kind == 'month-end' else 1
            if day.day != expected_day:
                raise _error('予約種別と暦月の日付が一致しません。')
            compare = (day + timedelta(days=1) if kind == 'month-end' else day).strftime('%Y-%m')
            if value['comparison_month'] != compare:
                raise _error('月次比較の対象月が一致しません。')
        else:
            raise _error('予約取得の種別を確認できません。')
        return dict(schedule_id=identifier, schedule_revision=revision, kind=kind,
                    scheduled_for=planned.isoformat(), slot_date=day.isoformat(),
                    include_pdfs=value['include_pdfs'], comparison_month=value['comparison_month'])
    except ScheduledRecordError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError):
        raise _error('予約日時・比較対象月を確認できません。') from None


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def _digest(value):
    return hashlib.sha256(_canonical({k: v for k, v in value.items() if k != 'sha256'})).hexdigest()


def occurrence_key(plan):
    return hashlib.sha256(_canonical(validate_plan(plan))).hexdigest()


def _link(path):
    info = path.lstat()
    return (path.is_symlink() or bool(getattr(path, 'is_junction', lambda: False)())
            or bool(getattr(info, 'st_file_attributes', 0) &
                    getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)))


def _directory(path, create=False):
    if path.exists() or path.is_symlink():
        if _link(path) or not path.is_dir():
            raise _error('予約取得の保存先にリンク・不正なフォルダーがあります。')
    elif create:
        path.mkdir(exist_ok=True)
        if _link(path) or not path.is_dir():
            raise _error('予約取得の保存先を安全に作成できません。')
    return path


def _root(base, create=False):
    current = Path(base).absolute()
    if not current.is_dir() or _link(current):
        raise _error('通常のアプリフォルダーを指定してください。')
    current = current.resolve()
    for part in ('data', 'scheduled'):
        current = _directory(current / part, create)
    return current


def _file_info(path):
    info = path.lstat()
    if (_link(path) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_size > _MAX_BYTES):
        raise _error('予約取得記録のファイルを安全に読み取れません。')
    return info


def _read_json(path):
    try:
        before = _file_info(path)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) |
                             getattr(os, 'O_NOFOLLOW', 0))
        try:
            opened = os.fstat(descriptor)
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
                raise _error()
            with os.fdopen(descriptor, 'rb', closefd=False) as handle:
                raw = handle.read(_MAX_BYTES + 1)
        finally:
            os.close(descriptor)
        after = _file_info(path)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or len(raw) > _MAX_BYTES):
            raise _error()

        def unique(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise _error('予約取得記録の項目が重複しています。')
                result[key] = item
            return result

        value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(_error()))
        checksum = value.get('sha256') if isinstance(value, dict) else None
        if (not isinstance(checksum, str) or not _HASH.fullmatch(checksum)
                or not hmac.compare_digest(checksum, _digest(value))):
            raise _error('予約取得記録のチェックサムが一致しません。')
        return value
    except ScheduledRecordError:
        raise
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise _error() from None


def _atomic_new(path, value):
    if path.exists() or path.is_symlink():
        raise _error('同じ予約取得記録を上書きできません。')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.scheduled_', suffix='.tmp',
                                         dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(_canonical(value)); handle.flush(); os.fsync(handle.fileno())
        if path.exists() or path.is_symlink():
            raise _error('予約取得記録が保存中に変更されました。')
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def _lock(root):
    path = root / '.scheduled.lock'
    if path.exists() or path.is_symlink():
        _file_info(path)
    try:
        with path.open('a+b') as handle:
            handle.seek(0)
            if not handle.read(1):
                handle.write(b'0'); handle.flush()
            handle.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise _error('別の予約取得記録の更新が実行中です。') from None
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except ScheduledRecordError:
        raise
    except OSError:
        raise _error('予約取得記録を保存できません。空き容量とアクセス権を確認してください。') from None


def _when(plan, observed_at):
    if isinstance(observed_at, datetime):
        observed_at = observed_at.isoformat()
    try:
        observed = _stamp(observed_at)
    except ValueError:
        raise _error('取得の実日時を確認できません。') from None
    planned = _stamp(plan['scheduled_for'])
    delay = (observed - planned).total_seconds()
    if observed.date().isoformat() != plan['slot_date'] or delay < 0:
        raise _error('別の日や予定前の取得値を予約完了として保存できません。')
    return observed.isoformat(), delay


def _validated_summary(value):
    try:
        return _summary(value, strict=True)
    except (ValueError, TypeError, KeyError, OverflowError):
        raise _error('口座サマリーの数値・未取得項目・実日時を確認できません。') from None


def _base_record(schema, plan, account, owner):
    return dict(schema=schema, version=1, account=_account(account), owner=_owner(owner), plan=plan)


def _check_base(value, schema, plan, account, owner, fields):
    if (not isinstance(value, dict) or set(value) != fields
            or value['schema'] != schema or type(value['version']) is not int or value['version'] != 1
            or value['account'] != _account(account) or value['owner'] != _owner(owner)
            or validate_plan(value['plan']) != value['plan'] or value['plan'] != plan):
        raise _error()


class ScheduledRunStore:
    def __init__(self, base, plan, owners):
        self.base = Path(base).absolute()
        self.plan = validate_plan(plan)
        self.key = occurrence_key(self.plan)
        if not isinstance(owners, dict) or not owners or set(owners) - set('ABCD'):
            raise _error('予約取得対象の名義設定を確認できません。')
        try:
            self.owners = {_account(account): _owner(owner) for account, owner in owners.items()}
        except ValueError:
            raise _error('予約取得対象の名義設定を確認できません。') from None
        _root(self.base)

    def _account(self, account):
        if account not in self.owners:
            raise _error('名義を確認した対象口座だけを使用してください。')
        return _account(account)

    def _folder(self, kind, account=None, create=False):
        root = _root(self.base, create)
        path = root
        for part in (kind, self.plan['schedule_id'], self.key):
            path = _directory(path / part, create)
        if account is not None:
            path = _directory(path / self._account(account), create)
        return path

    def _marker(self, kind, account, create=False):
        return self._folder(kind, create=create) / (self._account(account) + '.json')

    def _records(self, account):
        account = self._account(account)
        folder = self._folder('measurements', account)
        if not folder.exists():
            return []
        records = []
        fields = {'schema', 'version', 'account', 'owner', 'plan', 'attempt_id', 'summary',
                  'timing', 'delay_seconds', 'source', 'displayed_at_is_valuation', 'sha256'}
        for path in sorted(folder.iterdir()):
            if _link(path):
                raise _error()
            if path.name.startswith('.scheduled_') and path.suffix == '.tmp':
                continue
            if not path.is_file() or path.suffix != '.json' or not _ATTEMPT.fullmatch(path.stem):
                raise _error('予約取得記録に想定外のファイルがあります。')
            value = _read_json(path)
            _check_base(value, 'sbi-scheduled-summary', self.plan, account,
                        self.owners[account], fields)
            if value['attempt_id'] != path.stem:
                raise _error()
            summary = _validated_summary(value['summary'])
            _, delay = _when(self.plan, summary['observed_at'])
            if (summary != value['summary'] or value['timing'] != ('near-schedule' if delay <= 1800 else 'late')
                    or type(value['delay_seconds']) not in (int, float) or value['delay_seconds'] != delay
                    or value['source'] != SOURCE or value['displayed_at_is_valuation'] is not False):
                raise _error()
            records.append(value)
            if len(records) > _MAX_RECORDS:
                raise _error('予約取得記録の件数が安全な上限を超えています。')
        return records

    def preflight(self):
        for account in self.owners:
            self._records(account)
            self.pdf_done(account)
            self.held(account)

    def select(self, account):
        records = self._records(account)
        return min(records, key=lambda value: (not value['summary']['complete'],
            len(value['summary']['missing']), value['delay_seconds'], value['attempt_id'])) if records else None

    def save(self, account, summary):
        account = self._account(account)
        summary = _validated_summary(summary)
        _, delay = _when(self.plan, summary['observed_at'])
        value = _base_record('sbi-scheduled-summary', self.plan, account, self.owners[account])
        value.update(attempt_id=uuid4().hex, summary=summary,
                     timing='near-schedule' if delay <= 1800 else 'late', delay_seconds=delay,
                     source=SOURCE, displayed_at_is_valuation=False)
        value['sha256'] = _digest(value)
        root = _root(self.base, True)
        with _lock(root):
            self._records(account)
            path = self._folder('measurements', account, True) / (value['attempt_id'] + '.json')
            _atomic_new(path, value)
        return path

    def pdf_done(self, account):
        account = self._account(account)
        path = self._marker('pdf_completions', account)
        if not path.exists() and not path.is_symlink():
            return False
        value = _read_json(path)
        fields = {'schema', 'version', 'account', 'owner', 'plan', 'observed_at', 'saved', 'duplicate', 'sha256'}
        _check_base(value, 'sbi-scheduled-pdf', self.plan, account, self.owners[account], fields)
        observed, _ = _when(self.plan, value['observed_at'])
        if observed != value['observed_at'] or any(type(value[key]) is not int or value[key] < 0
                                                for key in ('saved', 'duplicate')):
            raise _error()
        return True

    def save_pdf(self, account, outcome, observed_at):
        account = self._account(account)
        if (not isinstance(outcome, dict) or outcome.get('complete') is not True
                or outcome.get('pdf_collected') is not True):
            raise _error('PDF全体の取得完了を確認できません。')
        counts = {key: outcome.get(key, 0) for key in ('saved', 'duplicate')}
        if any(type(number) is not int or number < 0 for number in counts.values()):
            raise _error('PDF取得件数を確認できません。')
        observed, _ = _when(self.plan, observed_at)
        value = _base_record('sbi-scheduled-pdf', self.plan, account, self.owners[account])
        value.update(observed_at=observed, **counts)
        value['sha256'] = _digest(value)
        root = _root(self.base, True)
        with _lock(root):
            path = self._marker('pdf_completions', account, True)
            if self.pdf_done(account):
                return path
            _atomic_new(path, value)
        return path

    def held(self, account):
        account = self._account(account)
        path = self._marker('attention', account)
        if not path.exists() and not path.is_symlink():
            return False
        _read_attention(path, self.plan, account, self.owners[account])
        return True

    def hold(self, account, code='MANUAL_AUTH_REQUIRED'):
        account = self._account(account)
        if not isinstance(code, str) or code not in MANUAL_CODES:
            raise _error('認証待ち理由を確認できません。')
        value = _base_record('sbi-scheduled-attention', self.plan, account, self.owners[account])
        value.update(code=code, recorded_at=datetime.now(JST).isoformat())
        value['sha256'] = _digest(value)
        root = _root(self.base, True)
        with _lock(root):
            path = self._marker('attention', account, True)
            if self.held(account):
                return path
            _atomic_new(path, value)
        return path

    def clear_attention(self, account):
        account = self._account(account)
        return clear_account_attention(self.base, account, self.owners[account])


def _read_attention(path, plan, account, owner):
    value = _read_json(path)
    fields = {'schema', 'version', 'account', 'owner', 'plan', 'code', 'recorded_at', 'sha256'}
    _check_base(value, 'sbi-scheduled-attention', plan, account, owner, fields)
    if (not isinstance(value['code'], str) or value['code'] not in MANUAL_CODES
            or _stamp(value['recorded_at']).isoformat() != value['recorded_at']
            or path.name != account + '.json' or path.parent.name != occurrence_key(plan)
            or path.parent.parent.name != plan['schedule_id']):
        raise _error()
    return value


def clear_account_attention(base, account, owner):
    """Explicit-success hook; validate all this owner's holds before deleting any."""
    account = _account(account); owner = _owner(owner)
    root = _root(base)
    attention = _directory(root / 'attention')
    if not attention.exists():
        return 0
    with _lock(root):
        paths = []
        for identifier in sorted(attention.iterdir()):
            if _link(identifier) or not identifier.is_dir() or not _ID.fullmatch(identifier.name):
                raise _error('認証待ち記録の保存先を確認できません。')
            for occurrence in sorted(identifier.iterdir()):
                if _link(occurrence) or not occurrence.is_dir() or not _HASH.fullmatch(occurrence.name):
                    raise _error('認証待ちの予定枠を確認できません。')
                path = occurrence / (account + '.json')
                if path.exists() or path.is_symlink():
                    value = _read_json(path)
                    plan = validate_plan(value.get('plan'))
                    _read_attention(path, plan, account, owner)
                    paths.append((path, plan))
                if len(paths) > _MAX_RECORDS:
                    raise _error('認証待ち記録の件数が安全な上限を超えています。')
        for path, plan in paths:
            _read_attention(path, plan, account, owner)
        for path, _ in paths:
            path.unlink()
        return len(paths)
