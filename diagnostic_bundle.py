"""Create a small, local diagnostic ZIP without credentials or PDF evidence.

Only selected text/JSON diagnostics are considered. Balances and account names
can remain after credential redaction, so users must review the ZIP before
sharing it. Screenshots are omitted because their pixels cannot be redacted by
this text-only helper. Nothing is sent over the network.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import platform
import re
import stat
import sys
import tempfile
from urllib.parse import urlsplit
from zipfile import ZIP_DEFLATED, ZipFile
import zlib

from version import APP_VERSION


MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_INPUT_BYTES = 64 * 1024 * 1024
MAX_BUNDLE_BYTES = 20 * 1024 * 1024
REDACTED = '[REDACTED]'
PRIVACY_NOTE = ('Account names, financial values and document titles may remain. '
                'Review before sharing. Screenshots, credentials, PDFs and the ledger are excluded.')
_SECRET_KEYS = {
    'accesstoken', 'refreshtoken', 'idtoken', 'token', 'password', 'passwd',
    'passphrase', 'clientsecret', 'apikey', 'secret', 'authorization',
    'proxyauthorization', 'cookie', 'setcookie', 'otp', 'onetimepassword',
    'verificationcode', 'authenticationcode', 'authcode', 'code', 'session', 'sessionid',
    'username', 'loginid', 'secretaccesskey', 'awssecretaccesskey',
    '認証コード', '認証番号', 'ワンタイムパスワード', 'パスワード',
}
_DUMP_KEYS = {'credentials', 'environment', 'environ', 'processenvironment',
              'privatekey', 'logins', 'credentialfile'}
_URL_KEYS = {'url', 'uri', 'href', 'src', 'location', 'requesturl', 'responseurl',
             'navigationurl', 'redirecturl', 'redirecturi'}
_URL = re.compile(r'https?://[^\s<>"\x27\[\]{}()]+', re.I)
_ASSIGNMENT = re.compile(
    r'(?im)(?<![\w])(?:access[_ -]?token|refresh[_ -]?token|id[_ -]?token|'
    r'client[_ -]?secret|api[_ -]?key|token|secret|password|passwd|passphrase|authorization|'
    r'proxy-authorization|set-cookie|cookie|otp|one[_ -]?time[_ -]?password|'
    r'verification[_ -]?code|authentication[_ -]?code|auth[_ -]?code|code|'
    r'session(?:[_ -]?id)?|認証コード|認証番号|ワンタイムパスワード|パスワード)'
    r'[\t ]*["\x27]?[\t ]*[:=：][\t ]*[^\r\n]*')
_OTP = re.compile(r'((?:認証コード|認証番号|ワンタイムパスワード|OTP)'
                  r'\s*(?:[:=：]\s*)?)([A-Za-z0-9-]{4,12})(?![A-Za-z0-9-])', re.I)
_BEARER = re.compile(r'\bBearer[\t ]+[A-Za-z0-9._~+/%=-]+', re.I)
_JWT = re.compile(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b')


def _key(value):
    return re.sub(r'[\s_-]', '', str(value)).lower()


def _url_host(match):
    try:
        parsed = urlsplit(match.group(0))
        host = parsed.hostname
        if not host or not re.fullmatch(r'[A-Za-z0-9.:-]+', host):
            return '[URL REDACTED]'
        if ':' in host:
            host = '[' + host + ']'
        return parsed.scheme.lower() + '://' + host.lower()
    except ValueError:
        return '[URL REDACTED]'


def _redact_text(value):
    # Remove the entire URL path as some sessions embed credentials in paths.
    value = _URL.sub(_url_host, value)
    value = _BEARER.sub('Bearer ' + REDACTED, value)
    value = _JWT.sub(REDACTED, value)
    value = re.sub(r'\bya29\.[A-Za-z0-9._~-]+', REDACTED, value)
    value = _ASSIGNMENT.sub(lambda m: m.group(0).split(':', 1)[0].split('=', 1)[0]
                            .split('：', 1)[0] + ': ' + REDACTED, value)
    return _OTP.sub(lambda m: m.group(1) + REDACTED, value)


def _sanitize_json(value):
    if isinstance(value, dict):
        if any(_key(k) in _DUMP_KEYS for k in value):
            raise ValueError('credential_or_environment_dump')
        cleaned = {}
        for name, item in value.items():
            if _key(name) in _SECRET_KEYS:
                result = REDACTED
            elif _key(name) in _URL_KEYS and isinstance(item, str):
                result = _redact_text(item) if _URL.fullmatch(item) else '[URL REDACTED]'
            else:
                result = _sanitize_json(item)
            cleaned[_redact_text(str(name))] = result
        return cleaned
    if isinstance(value, list):
        return [_sanitize_json(item) for item in value]
    if isinstance(value, str):
        _reject_credential_dump(value)
        return _redact_text(value)
    return value


def _reject_credential_dump(text):
    if '\x00' in text or re.search(r'-----BEGIN [^-]*(?:PRIVATE KEY|CREDENTIAL)', text):
        raise ValueError('credential_or_binary_content')
    if re.search(r'(?im)^\s*(?:export\s+)?(?:PATH|HOME|USERPROFILE|AWS_SECRET_ACCESS_KEY)\s*=', text):
        raise ValueError('environment_dump')
    if len(re.findall(r'(?m)^[A-Z][A-Z0-9_]{2,}\s*=', text)) >= 4:
        raise ValueError('environment_dump')
    # Unknown prose that appears to state a password without a parseable field
    # is safer to omit than to promise that a guessed redaction removed it.
    if re.search(r'(?im)^\s*(?:password|passwd|client_secret|refresh_token|access_token|'
                 r'id_token|token|api_key)\s+[^\s:=：]', text):
        raise ValueError('unstructured_credential_content')


def _sanitize(raw, suffix):
    text = raw.decode('utf-8-sig')
    _reject_credential_dump(text)
    if suffix == '.json':
        cleaned = _sanitize_json(json.loads(text))
        return json.dumps(cleaned, ensure_ascii=False, indent=2).encode('utf-8')
    return _redact_text(text).encode('utf-8')


def _checked(path, root, directory=False):
    relative = path.relative_to(root)
    current = root
    for component in relative.parts:
        current = current / component
        mode = current.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ValueError('symlink_refused')
    result = path.stat()
    expected = stat.S_ISDIR(result.st_mode) if directory else stat.S_ISREG(result.st_mode)
    if not expected or (not directory and result.st_nlink != 1):
        raise ValueError('non_regular_source')
    if not path.resolve().is_relative_to(root):
        raise ValueError('source_outside_application')
    return result


def _newest(directory, pattern, root, count, skipped, kind):
    if not directory.exists() and not directory.is_symlink():
        return []
    try:
        _checked(directory, root, directory=True)
        candidates = []
        for path in directory.glob(pattern):
            try:
                info = _checked(path, root)
                candidates.append((info.st_mtime_ns, path.name, path))
            except (OSError, ValueError):
                skipped.append({'kind': kind, 'reason': 'unsafe_source'})
        return [row[2] for row in sorted(candidates, reverse=True)[:count]]
    except (OSError, ValueError):
        skipped.append({'kind': kind, 'reason': 'unsafe_directory'})
        return []


def _sources(root, skipped):
    diagnostics = root / 'data' / 'diagnostics'
    rows = []
    for kind, pattern, count in (('auto', 'auto*.txt', 4),
                                  ('navigation', 'navigation*.json', 4),
                                  ('assets', 'assets*.json', 8)):
        for index, path in enumerate(_newest(diagnostics, pattern, root, count, skipped, kind), 1):
            rows.append((path, f'diagnostics/{kind}_{index:02}{path.suffix}'))
    # Images deliberately do not enter the ZIP: pixel contents are not redacted.
    for pattern in ('assets*.png', 'post_failure*.png'):
        if _newest(diagnostics, pattern, root, 1, [], 'screenshots'):
            skipped.append({'kind': 'screenshots', 'reason': 'images_require_manual_review'})
    jobs = root / 'data' / 'jobs'
    if jobs.exists() or jobs.is_symlink():
        try:
            _checked(jobs, root, directory=True)
            states = []
            for job in jobs.iterdir():
                try:
                    _checked(job, root, directory=True)
                    source = job / 'state.json'
                    info = _checked(source, root)
                    states.append((info.st_mtime_ns, job.name, source))
                except (OSError, ValueError):
                    skipped.append({'kind': 'job_state', 'reason': 'unsafe_or_missing_source'})
            for index, (_, _, source) in enumerate(sorted(states, reverse=True)[:5], 1):
                rows.append((source, f'jobs/job_{index:02}/state.json'))
        except (OSError, ValueError):
            skipped.append({'kind': 'job_state', 'reason': 'unsafe_directory'})
    for account in 'ABCDE':
        folder = root / 'data' / '電子ポストPDF' / account
        paths = _newest(folder, '取得結果*.json', root, 1, skipped, 'acquisition_audit')
        for path in paths:
            rows.append((path, f'acquisition/{account}.json'))
    return rows


@contextmanager
def _read_source(path, root, remaining_bytes=MAX_TOTAL_INPUT_BYTES):
    info = _checked(path, root)
    limit = min(MAX_SOURCE_BYTES, remaining_bytes)
    if info.st_size > limit:
        raise ValueError('source_too_large')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError('source_changed')
        with os.fdopen(fd, 'rb', closefd=False) as handle:
            raw = handle.read(limit + 1)
        after = _checked(path, root)
        if len(raw) > limit:
            raise ValueError('source_too_large')
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
            raise ValueError('source_changed')
        yield raw
    finally:
        os.close(fd)


def _entry_bound(name, data):
    # Conservative bound: local header + central-directory header + UTF-8 name
    # twice. zlib's wrapper makes this slightly larger than ZIP raw deflate.
    return 160 + 2 * len(name.encode('utf-8')) + len(zlib.compress(data, 6))


def create_bundle(base, destination, max_bytes=20 * 1024 * 1024):
    """Return path/files/skipped for a credential-redacted local diagnostic ZIP.

    Each source is <=4 MiB, total input is <=64 MiB, and the final compressed
    archive never exceeds max_bytes. A failed build leaves an existing target
    unchanged. This function neither reads private settings nor sends files.
    """
    base = Path(base)
    if base.is_symlink():
        raise ValueError('アプリのリンク先から診断を作成することはできません。')
    root = base.resolve(strict=True)
    if not root.is_dir():
        raise ValueError('アプリのフォルダを確認してください。')
    destination = Path(destination)
    if destination.suffix.lower() != '.zip' or destination.is_symlink():
        raise ValueError('診断ファイルはリンクではないZIPファイルを指定してください。')
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        raise ValueError('診断ZIPの上限サイズを確認してください。')
    max_bytes = min(max_bytes, MAX_BUNDLE_BYTES)
    metadata = {'app_version': APP_VERSION,
                'python_version': '.'.join(map(str, sys.version_info[:3])),
                'platform': platform.system(),
                'contains_account_information': True, 'privacy_note': PRIVACY_NOTE,
                'files_included': 99999999, 'files_skipped': 99999999}
    reserve = _entry_bound('manifest.json', json.dumps(metadata, ensure_ascii=False).encode('utf-8'))
    if reserve + 22 > max_bytes:
        raise ValueError('診断ZIPの上限が小さすぎます。1KB以上を指定してください。')
    skipped = []
    candidates = _sources(root, skipped)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix='.diagnostic_', suffix='.tmp', dir=destination.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    files = []
    used = 22
    input_bytes = 0
    try:
        with ZipFile(temporary, 'w', compression=ZIP_DEFLATED, compresslevel=6) as bundle:
            for path, archive_name in candidates:
                try:
                    with _read_source(path, root, MAX_TOTAL_INPUT_BYTES - input_bytes) as raw:
                        input_bytes += len(raw)
                        if input_bytes > MAX_TOTAL_INPUT_BYTES:
                            raise ValueError('total_input_limit')
                        cleaned = _sanitize(raw, path.suffix)
                    bound = _entry_bound(archive_name, cleaned)
                    if used + bound + reserve > max_bytes:
                        raise ValueError('archive_size_limit')
                    bundle.writestr(archive_name, cleaned)
                    used += bound
                    files.append(archive_name)
                except (OSError, ValueError, RecursionError):
                    # Fixed reasons only: exception text can contain credentials.
                    skipped.append({'kind': archive_name, 'reason': 'unreadable_unsafe_or_over_limit'})
            metadata.update(files_included=len(files), files_skipped=len(skipped))
            bundle.writestr('manifest.json', json.dumps(metadata, ensure_ascii=False, indent=2).encode('utf-8'))
        if temporary.stat().st_size > max_bytes:
            raise ValueError('診断ZIPが上限を超えました。元ファイルは変更していません。')
        if destination.is_symlink():
            raise ValueError('保存先がリンクに変更されました。保存していません。')
        os.replace(temporary, destination)
        return {'path': destination, 'files': files, 'skipped': skipped,
                'contains_account_information': True}
    finally:
        temporary.unlink(missing_ok=True)
