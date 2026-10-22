"""Offline, code-only ZIP updates. Close the application before using this tool.

Nothing from the ZIP is imported or executed during planning. Checksums detect
corruption; they do not authenticate a release. Use only a trusted source ZIP.
This module deliberately never reads or writes the application's data tree.
"""
from __future__ import annotations

import ast
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import unicodedata
from uuid import uuid4
import zipfile


MAX_ARCHIVE_BYTES=100*1024*1024
MAX_ENTRIES=6000
MAX_DECLARED_BYTES=512*1024*1024
MAX_CODE_BYTES=2*1024*1024
MAX_SELECTED_BYTES=24*1024*1024
MAX_MANIFEST_BYTES=512*1024
MANIFESTS={'update_manifest.json','配布ファイル_SHA256.json'}
PROTECTED_DIRS={'data','private','originals','backups','output','.git','.venv','venv','__pycache__','.aws','.codex','.agents'}
PROTECTED_SUFFIXES={'.enc','.secure','.dat','.sqlite','.sqlite3','.db','.key','.pem','.p12','.pfx'}
REFERENCE_JSON={'excel_layout.json','research_2026.json','security_names_default.json'}
DOCS={'readme.md','changelog.md','license','license.txt','pdf_acquisition_validation.md','はじめに.txt','はじめに.html'}
_VERSION=re.compile(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z')
_HASH=re.compile(r'[a-fA-F0-9]{64}\Z')
_DEVICE=re.compile(r'(?:con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(?:[ .].*)?\Z',re.I)


class CodeUpdateError(ValueError):
    """An update was rejected or could not be completed safely."""


@dataclass(frozen=True)
class UpdateFile:
    path:str
    content:bytes
    sha256:str
    previous_sha256:str|None


@dataclass(frozen=True)
class UpdatePlan:
    base:Path
    archive:Path
    current_version:str
    new_version:str
    source_root:str
    files:tuple[UpdateFile,...]
    code_file_count:int
    unchanged_count:int
    ignored_private_count:int
    ignored_other_count:int
    requirements_changed:bool
    manifest_verified:bool


@dataclass(frozen=True)
class UpdateResult:
    backup_dir:Path|None
    current_version:str
    new_version:str
    updated_files:tuple[str,...]
    requirements_changed:bool
    manifest_verified:bool


def _digest(content):return hashlib.sha256(content).hexdigest()


def _parts(name):
    if not isinstance(name,str) or not name or '\\' in name or '\x00' in name or name.startswith('/'):
        raise CodeUpdateError('ZIPに安全でないパスがあります。')
    path=name[:-1] if name.endswith('/') else name
    pieces=path.split('/')
    if any(not p or p in {'.','..'} or any(c in '<>:"|?*' for c in p) or p[-1:] in {' ','.'} or _DEVICE.fullmatch(p) or any(ord(c)<32 for c in p) for p in pieces):
        raise CodeUpdateError('ZIPに安全でないパスがあります。')
    return tuple(pieces)


def _fold(parts):return tuple(unicodedata.normalize('NFC',p).casefold() for p in parts)


def _protected(parts):
    return bool(set(_fold(parts))&PROTECTED_DIRS) or PurePosixPath(parts[-1]).suffix.casefold() in PROTECTED_SUFFIXES


def _allowed(parts):
    if _protected(parts):return False
    names=_fold(parts)
    if len(names)==2 and names[0]=='reference':return names[1] in REFERENCE_JSON
    if len(names)!=1:return False
    name=names[0]
    if name in {m.casefold() for m in MANIFESTS}:return True
    if PurePosixPath(name).suffix in {'.py','.bat'}:return True
    if re.fullmatch(r'requirements[A-Za-z0-9_-]*\.txt',name):return True
    if name in DOCS:return True
    return bool(re.fullmatch(r'(?:v\d+\.\d+\.\d+_readme\.txt|はじめに_v\d+\.\d+\.\d+\.txt|検証結果_v\d+\.\d+\.\d+\.md)',name))


def _plain_json(data):
    def unique(pairs):
        result={};seen=set()
        for key,value in pairs:
            if not isinstance(key,str) or unicodedata.normalize('NFC',key).casefold() in seen:
                raise CodeUpdateError('チェックサム一覧のキーが重複しています。')
            seen.add(unicodedata.normalize('NFC',key).casefold());result[key]=value
        return result
    try:return json.loads(data.decode('utf-8-sig'),object_pairs_hook=unique)
    except (ValueError,UnicodeError) as error:
        if isinstance(error,CodeUpdateError):raise
        raise CodeUpdateError('チェックサム一覧を読み取れません。') from None


def _version(content,strict=True):
    try:tree=ast.parse(content.decode('utf-8-sig'))
    except (SyntaxError,UnicodeError):raise CodeUpdateError('バージョン定義を読み取れません。') from None
    found=[]
    statements=tree.body if strict else ast.walk(tree)
    for node in statements:
        if isinstance(node,ast.Assign) and len(node.targets)==1 and isinstance(node.targets[0],ast.Name) and node.targets[0].id in ({'APP_VERSION'} if strict else {'APP_VERSION','VERSION'}):
            if not isinstance(node.value,ast.Constant) or not isinstance(node.value.value,str) or not _VERSION.fullmatch(node.value.value):
                raise CodeUpdateError('バージョンは数値3項の文字列で指定してください。')
            found.append(node.value.value)
        elif strict and not (isinstance(node,ast.Expr) and isinstance(node.value,ast.Constant) and isinstance(node.value.value,str)):
            raise CodeUpdateError('version.pyに実行処理を含む定義があります。')
    if len(found)!=1:raise CodeUpdateError('バージョン定義を一意に確認できません。')
    return found[0]


def _is_link(path):
    junction=getattr(path,'is_junction',None)
    return path.is_symlink() or bool(junction and junction())


def _target(base,relative):
    parts=_parts(relative)
    if not _allowed(parts):raise CodeUpdateError('コード以外の更新先は使用できません。')
    current=base
    for i,part in enumerate(parts):
        if _is_link(current):raise CodeUpdateError('更新先のリンク・ジャンクションは使用できません。')
        if current.exists():
            if not current.is_dir():raise CodeUpdateError('更新先の親がフォルダーではありません。')
            for sibling in current.iterdir():
                if sibling.name.casefold()==part.casefold() and sibling.name!=part:
                    raise CodeUpdateError('更新先に大文字・小文字の異なる同名ファイルがあります。')
        current=current/part
        if _is_link(current):raise CodeUpdateError('更新先のリンク・ジャンクションは使用できません。')
        if i==len(parts)-1 and current.exists() and not current.is_file():
            raise CodeUpdateError('更新先が通常のファイルではありません。')
    return current


def _base_path(base):
    original=Path(base).absolute()
    if _is_link(original) or not original.is_dir():raise CodeUpdateError('アプリの通常フォルダーを指定してください。')
    return original.resolve()


def _read_small(path,limit=MAX_CODE_BYTES):
    if path.stat().st_size>limit:raise CodeUpdateError('コードファイルがサイズ上限を超えています。')
    return path.read_bytes()


def _existing_version(base):
    version=_target(base,'version.py')
    if version.exists():return _version(_read_small(version))
    app=_target(base,'app.py')
    if not app.exists():raise CodeUpdateError('既存アプリのapp.pyがありません。')
    return _version(_read_small(app),strict=False)


def prepare_update(base,archive):
    """Return an immutable review plan without modifying the application.

    Protected payloads in a combined app/data ZIP are ignored, not decompressed.
    Equal-version refreshes are allowed; downgrades are rejected.
    """
    base=_base_path(base);archive=Path(archive).absolute()
    current_version=_existing_version(base)
    if not _target(base,'app.py').is_file():raise CodeUpdateError('既存アプリのapp.pyがありません。')
    if not archive.is_file() or archive.stat().st_size>MAX_ARCHIVE_BYTES:
        raise CodeUpdateError('ZIPがないか、100MiBの上限を超えています。')
    try:
        with zipfile.ZipFile(archive) as z:
            infos=z.infolist()
            if len(infos)>MAX_ENTRIES:raise CodeUpdateError('ZIP内のファイル数が上限を超えています。')
            entries={};folded={};total=0
            for info in infos:
                parts=_parts(info.orig_filename)
                if info.orig_filename!=info.filename:raise CodeUpdateError('ZIPのファイル名が不正です。')
                key=_fold(parts);mode=stat.S_IFMT(info.external_attr>>16)
                if mode not in {0,stat.S_IFREG,stat.S_IFDIR} or (mode==stat.S_IFDIR and not info.is_dir()):
                    raise CodeUpdateError('ZIP内のリンク・特殊ファイルは使用できません。')
                if key in folded:raise CodeUpdateError('ZIP内に同名・大文字小文字違いのパスがあります。')
                folded[key]=info;entries[parts]=info;total+=info.file_size
                if info.file_size>256*1024*1024 or total>MAX_DECLARED_BYTES or (info.file_size>1024*1024 and info.file_size/max(1,info.compress_size)>1000):
                    raise CodeUpdateError('ZIPの展開サイズ・圧縮率が上限を超えています。')
            for key,info in folded.items():
                if any(prefix in folded and not folded[prefix].is_dir() for prefix in (key[:i] for i in range(1,len(key)))):
                    raise CodeUpdateError('ZIP内のファイルとフォルダーのパスが競合しています。')
            roots=[]
            for parts,info in entries.items():
                if not _protected(parts) and not info.is_dir() and parts[-1].casefold()=='app.py' and _fold(parts[:-1]+('version.py',)) in folded:
                    roots.append(parts[:-1])
            if len(roots)!=1:raise CodeUpdateError('app.pyとversion.pyのあるアプリを一意に確認できません。')
            root=roots[0];selected={};ignored_private=ignored_other=0;selected_bytes=0
            for parts,info in entries.items():
                if info.is_dir():continue
                if _protected(parts):ignored_private+=1;continue
                relative=parts[len(root):] if parts[:len(root)]==root else ()
                if not relative or not _allowed(relative):
                    if _protected(relative or parts):ignored_private+=1
                    else:ignored_other+=1
                    continue
                limit=MAX_MANIFEST_BYTES if relative[-1] in MANIFESTS else MAX_CODE_BYTES
                selected_bytes+=info.file_size
                if info.flag_bits&1 or info.file_size>limit or selected_bytes>MAX_SELECTED_BYTES or (info.file_size>32768 and info.file_size/max(1,info.compress_size)>250):
                    raise CodeUpdateError('更新コードの暗号化・サイズ・圧縮率を確認してください。')
                data=z.read(info)
                if len(data)!=info.file_size:raise CodeUpdateError('更新コードのサイズが一致しません。')
                selected['/'.join(relative)]=data
            if 'app.py' not in selected or 'version.py' not in selected:
                raise CodeUpdateError('app.pyとversion.pyを標準の名前で含めてください。')
            new_version=_version(selected['version.py'])
            if tuple(map(int,new_version.split('.')))<tuple(map(int,current_version.split('.'))):
                raise CodeUpdateError('旧バージョンへの更新はできません。')
            verified=False
            for manifest in MANIFESTS:
                if manifest not in selected:continue
                obj=_plain_json(selected[manifest])
                if not isinstance(obj,dict):raise CodeUpdateError('チェックサム一覧の形式が不正です。')
                if manifest=='update_manifest.json':
                    if obj.get('version')!=new_version or not isinstance(obj.get('files'),dict):
                        raise CodeUpdateError('チェックサム一覧のバージョン・形式が一致しません。')
                    obj=obj['files']
                hashes={}
                for name,digest in obj.items():
                    parts=_parts(name)
                    if not isinstance(digest,str) or not _HASH.fullmatch(digest):raise CodeUpdateError('チェックサムの形式が不正です。')
                    hashes['/'.join(parts)]=digest.lower()
                for name,data in selected.items():
                    if name in MANIFESTS:continue
                    if hashes.get(name)!=_digest(data):raise CodeUpdateError('配布ファイルのチェックサムが一致しません。')
                for name in hashes:
                    if _allowed(_parts(name)) and name not in MANIFESTS and name not in selected:
                        raise CodeUpdateError('チェックサム一覧のコードファイルがZIPにありません。')
                verified=True
            changes=[];unchanged=0
            for name,data in sorted(selected.items()):
                target=_target(base,name)
                previous=_digest(_read_small(target)) if target.exists() else None
                digest=_digest(data)
                if _fold(_parts(name))==('reference','excel_layout.json') and previous is not None and previous!=digest:
                    raise CodeUpdateError('既存のExcel出力形式とZIPのexcel_layout.jsonが異なります。形式を保持するため更新を中止しました。')
                if previous==digest:unchanged+=1
                else:changes.append(UpdateFile(name,data,digest,previous))
            return UpdatePlan(base,archive,current_version,new_version,'/'.join(root),tuple(changes),len(selected),unchanged,ignored_private,ignored_other,
                              any(f.path.casefold().startswith('requirements') and f.path.casefold().endswith('.txt') for f in changes),verified)
    except CodeUpdateError:raise
    except (OSError,zipfile.BadZipFile,RuntimeError,NotImplementedError):
        raise CodeUpdateError('ZIPを安全に読み取れません。ファイルを確認してください。') from None


def _check_current(base,file):
    target=_target(base,file.path)
    current=_digest(_read_small(target)) if target.exists() else None
    if current!=file.previous_sha256:raise CodeUpdateError('確認後に既存コードが変わりました。ZIPの確認からやり直してください。')
    if _digest(file.content)!=file.sha256:raise CodeUpdateError('更新計画のチェックサムが一致しません。')
    if _fold(_parts(file.path))==('reference','excel_layout.json') and current is not None and current!=file.sha256:
        raise CodeUpdateError('既存のExcel出力形式を変更するコード更新はできません。')
    return target


def _sync_write(path,data):
    with path.open('xb') as handle:
        handle.write(data);handle.flush();os.fsync(handle.fileno())


@contextmanager
def _lock(backups):
    lock=backups/'.code_update.lock'
    if _is_link(lock):raise CodeUpdateError('更新ロックにリンクは使用できません。')
    with lock.open('a+b') as handle:
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
        except OSError:raise CodeUpdateError('別のコード更新が実行中です。終了してから実行してください。') from None
        try:yield
        finally:
            handle.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(),fcntl.LOCK_UN)


def _atomic_bytes(target,data,mode=None):
    target.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.family-code-',dir=target.parent)
    tmp=Path(name)
    try:
        with os.fdopen(fd,'wb') as handle:
            handle.write(data);handle.flush();os.fsync(handle.fileno())
        if mode is not None:tmp.chmod(mode)
        os.replace(tmp,target)
    finally:
        if tmp.exists():tmp.unlink()


def apply_update(plan):
    """Back up and apply a reviewed plan; preserve all nonselected files.

    Close the main application first. No package installation or Git operation
    is performed. Every overwritten code file has a verified recovery copy.
    """
    if not isinstance(plan,UpdatePlan):raise CodeUpdateError('確認済みの更新計画が必要です。')
    base=_base_path(plan.base)
    if _existing_version(base)!=plan.current_version:raise CodeUpdateError('確認後にアプリのバージョンが変わりました。')
    for file in plan.files:_check_current(base,file)
    if not plan.files:return UpdateResult(None,plan.current_version,plan.new_version,(),False,plan.manifest_verified)
    backups=base/'backups'
    if _is_link(backups) or (backups.exists() and not backups.is_dir()):raise CodeUpdateError('バックアップ先に通常フォルダーが必要です。')
    backups.mkdir(exist_ok=True)
    with _lock(backups):
        for file in plan.files:_check_current(base,file)
        backup=backups/('code_update_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'_'+uuid4().hex[:8])
        backup.mkdir()
        records={};touched=[];created_dirs=[]
        try:
            for file in plan.files:
                target=_check_current(base,file);mode=stat.S_IMODE(target.stat().st_mode) if target.exists() else None
                records[file.path]={'previous_sha256':file.previous_sha256,'new_sha256':file.sha256,'mode':mode}
                if file.previous_sha256 is not None:
                    copy=backup/'code'/file.path;copy.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(target,copy)
                    if _digest(_read_small(copy))!=file.previous_sha256:raise CodeUpdateError('コードのバックアップを検証できません。')
            manifest={'format':1,'from_version':plan.current_version,'to_version':plan.new_version,'files':records}
            _sync_write(backup/'manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2).encode('utf-8'))
            if _plain_json((backup/'manifest.json').read_bytes())!=manifest:raise CodeUpdateError('バックアップ一覧を検証できません。')
            for file in plan.files:
                target=_check_current(base,file)
                if not target.parent.exists():created_dirs.append(target.parent)
                touched.append(file)
                _atomic_bytes(target,file.content,records[file.path]['mode'])
                if _digest(_read_small(target))!=file.sha256:raise CodeUpdateError('更新後のコードを検証できません。')
            _sync_write(backup/'completed.json',json.dumps({'updated_files':[f.path for f in plan.files]},ensure_ascii=False,indent=2).encode('utf-8'))
        except BaseException as error:
            rollback_failed=[]
            for file in reversed(touched):
                try:
                    target=_target(base,file.path)
                    if file.previous_sha256 is None:
                        if target.exists():target.unlink()
                    elif not target.exists() or _digest(_read_small(target))!=file.previous_sha256:
                        copy=backup/'code'/file.path;old=_read_small(copy)
                        if _digest(old)!=file.previous_sha256:raise CodeUpdateError('復元用コピーが一致しません。')
                        _atomic_bytes(target,old,records[file.path]['mode'])
                        if _digest(_read_small(target))!=file.previous_sha256:raise CodeUpdateError('復元後のコードが一致しません。')
                except Exception:rollback_failed.append(file.path)
            for directory in reversed(created_dirs):
                try:directory.rmdir()
                except OSError:pass
            if rollback_failed:
                raise CodeUpdateError('更新を中止しました。一部コードの自動復元に失敗しました。アプリを起動せず、backups内のコード更新バックアップから復元してください。') from None
            if touched:raise CodeUpdateError('更新に失敗したため、コードを更新前に戻しました。データ・口座設定は変更していません。') from None
            if isinstance(error,CodeUpdateError):raise
            raise CodeUpdateError('バックアップの作成に失敗したため、コードを変更せず中止しました。') from None
        return UpdateResult(backup,plan.current_version,plan.new_version,tuple(f.path for f in plan.files),plan.requirements_changed,plan.manifest_verified)


def main():
    """Interactive local review; no downloads, authentication or app startup."""
    import tkinter as tk
    from tkinter import filedialog,messagebox
    root=tk.Tk();root.withdraw()
    try:
        archive=filedialog.askopenfilename(title='信頼できる入手元から保存したコードZIPを選択',filetypes=[('ZIPファイル','*.zip')])
        if not archive:return 0
        plan=prepare_update(Path(__file__).resolve().parent,archive)
        review=(f'コード更新：v{plan.current_version} → v{plan.new_version}\n'
                f'コード対象 {plan.code_file_count} ファイル／変更 {len(plan.files)}／同一 {plan.unchanged_count}\n'
                f'データ・認証ファイル {plan.ignored_private_count} 件、その他 {plan.ignored_other_count} 件は更新対象外です。\n'
                f'チェックサム一覧：{"一致" if plan.manifest_verified else "同梱なし"}（入手元の真正性を証明するものではありません）\n\n'
                '家族売買記録アプリを終了し、信頼できる入手元のZIPであることを確認してください。\n'
                'コードをバックアップして更新します。data・口座設定・PDF・出力Excelは上書きしません。\n'
                '更新を適用しますか？')
        if not messagebox.askyesno('コード更新の確認',review,default='no'):return 0
        result=apply_update(plan)
        if not result.updated_files:messagebox.showinfo('コード更新','変更対象のコードはありません。');return 0
        message=f'v{result.new_version} に更新しました。\n{len(result.updated_files)} ファイルを変更しました。\n'
        message+='コードバックアップ：backups/'+result.backup_dir.name+'\n'
        if result.requirements_changed:message+='依存関係の指定が変わりました。起動前に setup_all_windows.bat を実行してください。\n'
        message+='起動は start_windows.bat から行ってください。'
        messagebox.showinfo('コード更新完了',message);return 0
    except Exception as error:
        # Do not expose a path, source code, URL or payload through an exception.
        messagebox.showerror('コード更新を中止',str(error) if isinstance(error,CodeUpdateError) else '処理を完了できませんでした。アプリを終了し、ZIPとフォルダーへのアクセス権を確認してください。')
        return 1
    finally:root.destroy()


if __name__=='__main__':raise SystemExit(main())
