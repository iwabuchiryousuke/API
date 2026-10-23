"""Windows-only encrypted storage for optional SBI login autofill.

Credentials are protected by Windows DPAPI and can be decrypted only by the
same Windows user on the same PC.  They are never accepted on the command line,
written to logs, included in Excel, or copied into an application ZIP.
"""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path


class CredentialError(ValueError):
    pass


class AuthenticationRequired(CredentialError):
    """A known credential/manual-auth state that retrying cannot resolve."""
    code = 'MANUAL_AUTH_REQUIRED'
    retryable = False


_ENTROPY = b'SBI-family-manager-login-v1'
_MAGIC = b'SBI-DPAPI-1\n'


class _DataBlob(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD),
                ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


def _input_blob(data):
    buffer = ctypes.create_string_buffer(data)
    blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    return blob, buffer


def _dpapi(data, *, protect):
    if os.name != 'nt':
        raise CredentialError('ログイン情報の安全な保存はWindows版でのみ利用できます。')
    source, source_buffer = _input_blob(data)
    entropy, entropy_buffer = _input_blob(_ENTROPY)
    destination = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob), wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob), wintypes.LPVOID, wintypes.LPVOID,
        wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob), wintypes.LPVOID, wintypes.LPVOID,
        wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    flags = 0x01  # CRYPTPROTECT_UI_FORBIDDEN
    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(source), 'SBI家族売買記録', ctypes.byref(entropy),
            None, None, flags, ctypes.byref(destination))
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(source), None, ctypes.byref(entropy),
            None, None, flags, ctypes.byref(destination))
    # Keep the backing buffers alive until the Win32 call has returned.
    del source_buffer, entropy_buffer
    if not ok:
        raise CredentialError('Windowsでログイン情報を暗号化または復号できませんでした。')
    try:
        return ctypes.string_at(destination.pbData, destination.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(destination.pbData, wintypes.HLOCAL))


def _validate_account(account):
    if account not in tuple('ABCDE'):
        raise CredentialError('口座はA～Eから選んでください。')


def credential_path(data_directory, account):
    _validate_account(account)
    return Path(data_directory) / 'private' / f'login_{account}.dat'


def has_credentials(data_directory, account):
    return credential_path(data_directory, account).is_file()


def save_credentials(data_directory, account, username, password):
    _validate_account(account)
    username = str(username).strip()
    password = str(password)
    if not username or len(username) > 100:
        raise CredentialError('ユーザーネームを1～100文字で入力してください。')
    if not password or len(password) > 256:
        raise CredentialError('ログインパスワードを1～256文字で入力してください。')
    payload = json.dumps(
        {'version': 1, 'username': username, 'password': password},
        ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    protected = _dpapi(payload, protect=True)
    target = credential_path(data_directory, account)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.tmp')
    temporary.write_bytes(_MAGIC + base64.b64encode(protected))
    temporary.replace(target)
    return target


def load_credentials(data_directory, account):
    """Return optional saved credentials; unusable local content needs setup."""
    target = credential_path(data_directory, account)
    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as error:
        # A temporary read/locking failure does not prove broken credentials.
        raise CredentialError('保存済みログイン情報を読み取れません。保存先のアクセス状態を確認してください。') from error
    try:
        if not raw.startswith(_MAGIC):
            raise ValueError
        payload = _dpapi(base64.b64decode(raw[len(_MAGIC):], validate=True), protect=False)
        value = json.loads(payload.decode('utf-8'))
        if (not isinstance(value,dict) or type(value.get('version')) is not int or value['version'] != 1
                or not isinstance(value.get('username'),str) or not 1<=len(value['username'].strip())<=100
                or not isinstance(value.get('password'),str) or not 1<=len(value['password'])<=256):
            raise ValueError
        return value['username'], value['password']
    except Exception as error:
        raise AuthenticationRequired(
            '保存済みログイン情報を開けません。このPC・Windowsユーザーで設定し直してください。'
        ) from error


def delete_credentials(data_directory, account):
    target = credential_path(data_directory, account)
    if target.exists():
        target.unlink()
        return True
    return False
