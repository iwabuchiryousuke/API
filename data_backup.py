"""Back up an existing installation before an upgrade touches user data.

Private settings are copied as encrypted bytes. SQLite uses its online backup
API so the saved database is consistent even when WAL files exist.
"""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile


def backup_before_upgrade(base, version):
    base = Path(base)
    source = base / 'data'
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]+){2}', version):
        raise ValueError('Invalid backup version')
    if not source.exists() or not any(source.iterdir()):
        return None
    destination = base / 'backups'
    destination.mkdir(exist_ok=True)
    # Only a fully written manifest establishes a completed upgrade backup.
    for previous in sorted(destination.glob('before_v' + version + '_*')):
        marker = previous / 'manifest.json'
        if marker.is_file():
            try:
                metadata = json.loads(marker.read_text(encoding='utf-8'))
                hashes = metadata.get('sha256', {})
                if (metadata.get('version') == version and metadata.get('complete')
                        and isinstance(hashes, dict) and hashes
                        and all(not Path(name).is_absolute() and '..' not in Path(name).parts
                                and hashlib.sha256((previous / 'data' / name).read_bytes()).hexdigest() == digest
                                for name, digest in hashes.items())):
                    return previous
            except (OSError, ValueError, TypeError, AttributeError):
                # Preserve an incomplete/corrupt backup and make a fresh one.
                continue
    scratch = Path(tempfile.mkdtemp(prefix='.backup_', dir=destination))
    try:
        copied = scratch / 'data'
        copied.mkdir()
        entries = list(source.rglob('*'))
        if source.is_symlink() or any(p.is_symlink() for p in entries):
            raise OSError('バックアップ対象にリンクがあります。dataを確認してください。')
        for path in entries:
            target = copied / path.relative_to(source)
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif path.name in ('ledger.sqlite3-wal', 'ledger.sqlite3-shm', 'ledger.sqlite3-journal'):
                continue
            elif path == source / 'ledger.sqlite3':
                reader = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
                writer = sqlite3.connect(target)
                try:
                    reader.backup(writer)
                    if writer.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise OSError('バックアップした台帳の整合性を確認できません。')
                finally:
                    writer.close()
                    reader.close()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
        hashes = {p.relative_to(copied).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in copied.rglob('*') if p.is_file()}
        (scratch / 'manifest.json').write_text(json.dumps(
            {'version': version, 'complete': True, 'sha256': hashes},
            ensure_ascii=False, indent=2), encoding='utf-8')
        target = destination / ('before_v' + version + '_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
        os.replace(scratch, target)
        return target
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise
