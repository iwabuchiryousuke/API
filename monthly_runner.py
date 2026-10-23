"""Unattended, owner-bound monthly observations, separate from trade imports.

Calendar slots never backdate an acquisition. All accounts' summaries precede
the optional PDF phase. Only a fresh, validated summary can enter the ledger;
the staging database is never copied over the user's database.
"""
from contextlib import ExitStack, contextmanager
from datetime import datetime
import argparse
import csv
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unicodedata
import uuid

from ledger import Store, LedgerError
from monthly_schedule import JST, ROLES, due_slot, next_slots, _aware
from monthly_records import (REQUIRED, MonthlyRecordError, comparison, load_measurements,
                             save_measurement, select_measurement)
from acquisition_journal import _observation
from monthly_attention import (attention_required, blocked, clear_attention,
                               save_attention, MANUAL_CODES)
from update_workflow import exclusive, Cancelled, UpdateError
from version import APP_VERSION

ACCOUNTS = tuple('ABCD')
BASE = Path(__file__).resolve().parent


def _now():
    return datetime.now(JST)


def _owner(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 50:
        raise LedgerError('A～DすべてのPDF名義を先に設定してください。')
    result = ''.join(c for c in unicodedata.normalize('NFKC', value)
                     if not c.isspace() and unicodedata.category(c) != 'Cf')
    if not result:
        raise LedgerError('PDF名義を確認してください。')
    return result


def _safe(path, directory=False):
    """Reject existing links/reparse points, including Windows junctions."""
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if (path.is_symlink() or getattr(info, 'st_file_attributes', 0) &
                getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0) or
                (not path.is_dir() if directory else not stat.S_ISREG(info.st_mode)) or
                (not directory and info.st_nlink != 1)):
            raise LedgerError('保存先に通常のファイル・フォルダーを使用してください。')


def _folder(base, *parts):
    current = Path(base).absolute()
    _safe(current, True)
    for part in parts:
        current = current / part
        _safe(current, True)
        current.mkdir(exist_ok=True)
        _safe(current, True)
    return current


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _digest(value):
    return hashlib.sha256(_canonical({k: v for k, v in value.items()
                                     if k != 'sha256'})).hexdigest()


def _atomic(path, content):
    _safe(path)
    descriptor, temporary = tempfile.mkstemp(prefix='.monthly_', suffix='.tmp',
                                            dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _safe(path)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _json(path, value):
    _atomic(path, _canonical(value))


def _marker_path(base, slot, account):
    folder = _folder(base, 'data', 'scheduler', 'pdf_completions', slot['slot_date'])
    return folder / (account + '.json')


def _pdf_done(base, slot, account, owner):
    path = _marker_path(base, slot, account)
    if not path.exists() and not path.is_symlink():
        return False
    _safe(path)
    if path.stat().st_size > 8192:
        raise LedgerError('PDF完了記録のサイズを確認できません。')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        expected = {'schema', 'version', 'account', 'owner', 'slot_date', 'role',
                    'observed_at', 'saved', 'duplicate', 'sha256'}
        valid = (isinstance(value, dict) and set(value) == expected and
                 value['schema'] == 'sbi-monthly-pdf' and value['version'] == 1 and
                 value['account'] == account and value['owner'] == _owner(owner) and
                 value['slot_date'] == slot['slot_date'] and
                 value['role'] == slot['role'] and
                 _aware(value['observed_at']).date().isoformat() == slot['slot_date'] and
                 all(type(value[k]) is int and value[k] >= 0 for k in ('saved', 'duplicate')) and
                 hmac.compare_digest(str(value['sha256']), _digest(value)))
    except (ValueError, TypeError, KeyError, OverflowError):
        valid = False
    if not valid:
        raise LedgerError('PDF完了記録の名義・日時・整合性を確認できません。原本を保持しました。')
    return True


def _save_pdf_done(base, slot, account, owner, outcome, observed_at):
    if _aware(observed_at).date().isoformat() != slot['slot_date']:
        raise LedgerError('PDF取得が翌日になりました。予定日の完了扱いにはしません。')
    counts = {k: outcome.get(k, 0) for k in ('saved', 'duplicate')}
    if any(type(v) is not int or v < 0 for v in counts.values()):
        raise LedgerError('PDF取得件数を確認できません。')
    value = dict(schema='sbi-monthly-pdf', version=1, account=account,
                 owner=_owner(owner), slot_date=slot['slot_date'], role=slot['role'],
                 observed_at=_aware(observed_at).isoformat(), **counts)
    value['sha256'] = _digest(value)
    _json(_marker_path(base, slot, account), value)


@contextmanager
def _stage(live):
    with tempfile.TemporaryDirectory(prefix='sbi_monthly_') as directory:
        stage = Store(Path(directory) / 'data')
        try:
            live.db.backup(stage.db)
            yield stage
        finally:
            stage.close()


def _rows(store, account):
    if not store.db.execute("SELECT 1 FROM sqlite_master WHERE name='account_observations'").fetchone():
        return []
    return list(store.db.execute(
        'SELECT kind,observed_at,payload FROM account_observations WHERE account=? AND kind=?',
        (account, '口座サマリー')))


def _fresh(stage, account, before, since, until):
    accepted = []
    for row in _rows(stage, account):
        if (row['kind'], row['observed_at'], row['payload']) in before:
            continue
        item = _observation(dict(kind=row['kind'], observed_at=row['observed_at'],
                                 payload=json.loads(row['payload'])), strict=True)
        if since <= _aware(item['observed_at']) <= until:
            accepted.append(item)
    if not accepted:
        raise LedgerError('今回確認できた新しい口座サマリーがありません。取得成功とは扱いません。')
    return max(accepted, key=lambda item: _aware(item['observed_at']))


def _from_record(record):
    summary = record['summary']
    return _observation(dict(kind='口座サマリー', observed_at=summary['observed_at'],
        payload=dict(values=summary['values'], observed_at=summary['observed_at'],
                     displayed_at=summary['displayed_at'], source='口座サマリー')), strict=True)


def _append_live(live, account, owner, item):
    """Only append verified observations, with a backup and a transactional owner check."""
    item = _observation(item, strict=True)
    exists = live.db.execute("SELECT 1 FROM sqlite_master WHERE name='account_observations'").fetchone()
    if exists:
        row = live.db.execute('SELECT payload FROM account_observations WHERE account=? AND kind=? AND observed_at=?',
                              (account, item['kind'], item['observed_at'])).fetchone()
        if row and json.loads(row[0]) == item['payload']:
            # Still check ownership even when no write is needed.
            current = live.db.execute('SELECT owner_name FROM accounts WHERE id=?', (account,)).fetchone()
            if not current or _owner(current[0]) != _owner(owner):
                raise LedgerError('取得中に口座の名義設定が変わりました。保存を停止しました。')
            return
    live.backup()  # Must precede BEGIN IMMEDIATE (SQLite backup cannot run in its write txn).
    try:
        live.db.execute('BEGIN IMMEDIATE')
        current = live.db.execute('SELECT owner_name FROM accounts WHERE id=?', (account,)).fetchone()
        if not current or _owner(current[0]) != _owner(owner):
            raise LedgerError('取得中に口座の名義設定が変わりました。保存を停止しました。')
        live.db.execute('CREATE TABLE IF NOT EXISTS account_observations(account TEXT,kind TEXT,observed_at TEXT,payload TEXT,PRIMARY KEY(account,kind,observed_at))')
        row = live.db.execute('SELECT payload FROM account_observations WHERE account=? AND kind=? AND observed_at=?',
                              (account, item['kind'], item['observed_at'])).fetchone()
        if row:
            if json.loads(row[0]) != item['payload']:
                raise LedgerError('同じ取得時刻の資産記録が一致しません。既存値を保持しました。')
        else:
            live.db.execute('INSERT INTO account_observations VALUES (?,?,?,?)',
                            (account, item['kind'], item['observed_at'],
                             json.dumps(item['payload'], ensure_ascii=False, allow_nan=False)))
        live.db.commit()
    except BaseException:
        live.db.rollback()
        raise


def _summary(item):
    payload = item['payload']
    missing = [key for key in REQUIRED if payload['values'].get(key) is None]
    return dict(complete=not missing, missing=missing, values=payload['values'],
                observed_at=item['observed_at'], displayed_at=payload['displayed_at'])


def _cell(value):
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
        return "'" + value
    return '' if value is None else value


def export_comparison(base, value, labels=None):
    """Separate Excel-readable CSV; never alters the existing trade workbook."""
    month = value['month']
    if len(month) != 7 or datetime.strptime(month, '%Y-%m').strftime('%Y-%m') != month:
        raise LedgerError('比較対象月を確認してください。')
    target = _folder(base, 'output', 'monthly') / (month + '_資産比較.csv')
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(['対象月', '口座', '口座名', '判定', '月末予定', '月末実取得時刻',
        '月初予定', '月初実取得時刻', '月末株式表示額(円)', '月初株式表示額(円)',
        '株式増減(円)', '月末現金合計(円)', '月初現金合計(円)', '現金増減(円)',
        '月末株式+現金(円)', '月初株式+現金(円)', '株式+現金増減(円)',
        '月末証券預り金(円)', '月末スウィープ等(円)', '月初証券預り金(円)',
        '月初スウィープ等(円)', '月末画面表示時刻(評価基準ではない)',
        '月初画面表示時刻(評価基準ではない)', '説明'])
    for account, row in value['accounts'].items():
        before, after = row['month_end'], row['month_start']
        bv = before['summary']['values'] if before else {}
        av = after['summary']['values'] if after else {}
        totals = [sum(v[key] for key in REQUIRED) if all(v.get(key) is not None for key in REQUIRED)
                  else None for v in (bv, av)]
        writer.writerow([_cell(v) for v in [month, account, (labels or {}).get(account, row['owner']),
            row['status'], row['scheduled_for']['month-end'], row['observed_at']['month-end'],
            row['scheduled_for']['month-start'], row['observed_at']['month-start'],
            bv.get('summary_stocks'), av.get('summary_stocks'), row['deltas']['summary_stocks'],
            bv.get('cash_total'), av.get('cash_total'), row['deltas']['cash_total'], *totals,
            totals[1]-totals[0] if all(v is not None for v in totals) else None,
            bv.get('broker_cash'), bv.get('bank'), av.get('broker_cash'), av.get('bank'),
            before['summary']['displayed_at'] if before else None,
            after['summary']['displayed_at'] if after else None, value['note']]])
    _atomic(target, stream.getvalue().encode('utf-8-sig'))
    _json(_folder(base, 'data', 'monthly', 'comparisons') / (month + '.json'), value)
    return str(target)


def _error(error):
    # Raw browser/network errors can contain authenticated URLs. Keep reports secret-free.
    if isinstance(error, OSError):
        return 'ファイルの保存・バックアップに失敗しました。空き容量とアクセス権を確認してください。'
    if isinstance(error, (LedgerError, MonthlyRecordError)):
        text = str(error)
        if '://' not in text and len(text) <= 500:
            return text
    return '取得を完了できませんでした。Gmail連携・SBIの追加認証・通信とローカル診断を確認してください。'


def run_monthly(base, *, role='auto', now=None, authenticate=None, acquire=None,
                playwright=None, clock=None, cancelled=lambda: False,
                progress=lambda _: None, test=False, include_pdfs=None):
    base = Path(base).absolute()
    initial = _aware(now) if now is not None else _now()
    clock = clock or _now
    slot = due_slot(initial, role)
    result = dict(status='skipped', exit_code=0, slot=slot, test=bool(test),
                  accounts={}, comparison=None, comparison_csv=None, run_path=None,
                  version=APP_VERSION, started_at=initial.isoformat())
    if not test and slot is None:
        result['note'] = '今日は対象日でないか、16時前です。過去の予定へ遡って取得しません。'
        return result
    try:
        _safe(base, True)
        _safe(base / 'data', True)
        _safe(base / 'data' / 'ledger.sqlite3')
        if not (base / 'data' / 'ledger.sqlite3').is_file():
            raise LedgerError('先にアプリを起動して口座名義・SBI・Gmail連携を設定してください。')
        if acquire is None:
            # Existing acquisition modules own their install-relative credentials and PDF paths.
            if base.resolve() != BASE:
                raise LedgerError('自動取得は設定済みアプリのフォルダーから実行してください。')
            from auto_login import run_one
            acquire = run_one
        if authenticate is None:
            from gmail_auth import service
            authenticate = service  # Never starts an OAuth consent browser in the background.
        _safe(base / 'data' / 'update.lock')
        with exclusive(base / 'data' / 'update.lock'), ExitStack() as stack:
            # A first run may come from Task Scheduler before the GUI has ever
            # opened this version. Preserve encrypted settings and all data
            # before Store migrations or Gmail's token refresh can write.
            from data_backup import backup_before_upgrade
            result['upgrade_backup'] = str(backup_before_upgrade(base, APP_VERSION) or '')
            live = Store(base / 'data')
            stack.callback(live.close)
            profiles = {row['id']: row for row in live.accounts()}
            owners = {a: _owner(profiles[a].get('owner_name')) for a in ACCOUNTS}
            labels = {a: profiles[a]['label'] for a in ACCOUNTS}
            if not test:
                load_measurements(base, accounts=ACCOUNTS, owners=owners)  # Preflight every owner/hash.
                for account in ACCOUNTS:
                    _pdf_done(base, slot, account, owners[account]) if slot['include_pdfs'] else None
            holds = {a: blocked(base, slot, a, owners[a]) for a in ACCOUNTS} if not test else {}
            sessions = {}
            browser_api = playwright

            def check():
                if cancelled():
                    raise Cancelled()

            def resources(account):
                nonlocal browser_api
                check()
                check_owner(account)
                if account not in sessions:
                    sessions[account] = authenticate(base / 'data', account)
                if browser_api is None:
                    from playwright.sync_api import sync_playwright
                    browser_api = stack.enter_context(sync_playwright())
                return browser_api, sessions[account]

            def check_owner(account):
                row = live.db.execute('SELECT owner_name FROM accounts WHERE id=?', (account,)).fetchone()
                if not row or _owner(row[0]) != owners[account]:
                    raise LedgerError('取得中に口座の名義設定が変わりました。保存を停止しました。')

            def hold(account, error):
                if test or not attention_required(error):
                    return False
                holds[account] = True
                code = getattr(error, 'code', 'MANUAL_AUTH_REQUIRED')
                save_attention(base, slot, account, owners[account],
                               code=code if code in MANUAL_CODES else 'MANUAL_AUTH_REQUIRED')
                return True

            def report():
                folder = _folder(base, 'data', 'scheduler', 'test_runs' if test else 'runs')
                if result['run_path'] is None:
                    result['run_path'] = str(folder / (uuid.uuid4().hex + '.json'))
                result['finished_at'] = _aware(clock()).isoformat()
                _json(Path(result['run_path']), result)
                _json(folder.parent / ('latest_test_run.json' if test else 'latest_run.json'), result)

            result.update(status='running', exit_code=1)
            for account in ACCOUNTS:
                result['accounts'][account] = dict(asset_status='pending', pdf_status='not-requested')
            report()
            try:
                for account in ACCOUNTS:
                    check()
                    state = result['accounts'][account]
                    progress(f'口座{account}：口座サマリーの株式・現金を確認します。')
                    try:
                        record = select_measurement(base, account, slot, owners[account]) if not test else None
                        if record and record['summary']['complete']:
                            _append_live(live, account, owners[account], _from_record(record))
                            state.update(asset_status='reused', observed_at=record['summary']['observed_at'],
                                         timing=record['timing'])
                        elif holds.get(account):
                            state.update(asset_status='needs-attention',
                                error='認証・設定の確認待ちです。修正後にテスト取得で確認してください。')
                        else:
                            pw, session = resources(account)
                            since = _aware(clock())
                            with _stage(live) as stage:
                                before = {tuple(row) for row in _rows(stage, account)}
                                outcome = acquire(pw, account, stage, gmail_session=session,
                                    documents=False, capture_assets=True, summary_only=True,
                                    headless=True, target_dates=(), progress=progress, cancelled=cancelled)
                                check()
                                if not isinstance(outcome, dict) or outcome.get('complete') is not True:
                                    raise LedgerError('口座サマリー取得の完了を確認できません。')
                                item = _fresh(stage, account, before, since, _aware(clock()))
                            summary = _summary(item)
                            if not test:
                                measurement = save_measurement(base, account, owners[account], slot, summary)
                                state['measurement_path'] = str(measurement)
                            state.update(observed_at=summary['observed_at'], missing=summary['missing'])
                            if test:
                                state['summary'] = summary
                            _append_live(live, account, owners[account], item)
                            state['asset_status'] = 'complete' if summary['complete'] else 'partial'
                            if test and summary['complete']:
                                state['attention_cleared'] = clear_attention(base, account, owners[account])
                    except Cancelled:
                        raise
                    except Exception as error:
                        check()
                        state.update(asset_status='failed', error=_error(error))
                        if hold(account, error):
                            state['asset_status'] = 'needs-attention'
                    report()
                pdfs = (bool(include_pdfs) if include_pdfs is not None else
                        bool(slot and slot['include_pdfs'] and not test))
                if pdfs:
                    for account in ACCOUNTS:
                        check()
                        state = result['accounts'][account]
                        progress(f'口座{account}：PDFを取得・保存します（取引台帳への反映は行いません）。')
                        try:
                            check_owner(account)
                            if holds.get(account):
                                state['pdf_status'] = 'blocked'
                            elif not test and _pdf_done(base, slot, account, owners[account]):
                                state['pdf_status'] = 'reused'
                            else:
                                pw, session = resources(account)
                                with _stage(live) as stage:
                                    outcome = acquire(pw, account, stage, gmail_session=session,
                                        documents=True, capture_assets=False, headless=True,
                                        target_dates=(), progress=progress, cancelled=cancelled)
                                check()
                                check_owner(account)
                                if (not isinstance(outcome, dict) or outcome.get('complete') is not True
                                        or outcome.get('pdf_collected') is not True):
                                    raise LedgerError('PDF一覧全体の取得完了を確認できません。')
                                if not test:
                                    _save_pdf_done(base, slot, account, owners[account], outcome, clock())
                                state.update(pdf_status='complete', saved=outcome.get('saved', 0),
                                             duplicate=outcome.get('duplicate', 0))
                        except Cancelled:
                            raise
                        except Exception as error:
                            check()
                            state.update(pdf_status='failed', pdf_error=_error(error))
                            if hold(account, error):
                                state['pdf_status'] = 'blocked'
                        report()
                if not test:
                    result['comparison'] = comparison(base, slot['comparison_month'], owners)
                    result['comparison_csv'] = export_comparison(base, result['comparison'], labels)
                complete = all(state['asset_status'] in ('complete', 'reused') and
                    state['pdf_status'] in ('not-requested', 'complete', 'reused')
                    for state in result['accounts'].values())
                result.update(status='completed' if complete else 'partial', exit_code=0 if complete else 1)
            except (Cancelled, KeyboardInterrupt):
                result.update(status='cancelled', exit_code=1, error='中止しました。保存済みの記録は保持しています。')
            except Exception as error:
                result.update(status='failed', exit_code=1, error=_error(error))
            report()
    except Exception as error:
        result.update(status='busy' if isinstance(error, UpdateError) and error.code == 'BUSY' else 'failed',
                      exit_code=1, error=_error(error))
        # A preflight failure must replace an old success indication, too. Never
        # overwrite an active job's status when its shared lock is held.
        if result['status'] != 'busy' and (base / 'data' / 'ledger.sqlite3').is_file():
            try:
                with exclusive(base / 'data' / 'update.lock'):
                    folder = _folder(base, 'data', 'scheduler', 'test_runs' if test else 'runs')
                    if result['run_path'] is None:
                        result['run_path'] = str(folder / (uuid.uuid4().hex + '.json'))
                    result['finished_at'] = _aware(clock()).isoformat()
                    _json(Path(result['run_path']), result)
                    _json(folder.parent / ('latest_test_run.json' if test else 'latest_run.json'), result)
            except Exception:
                result['report_error'] = '実行結果をファイルへ保存できませんでした。'
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='月末・1日16:00 JSTの口座サマリー集計。月初はPDFも保存します。')
    parser.add_argument('--role', choices=ROLES, default='auto')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--scheduled', action='store_true')
    mode.add_argument('--test', action='store_true', help='現在の資産を確認。月次本番の記録・完了扱いにはしません。')
    parser.add_argument('--include-pdfs', action='store_true', default=None)
    parser.add_argument('--dry-run', action='store_true', help='予定表示だけ。設定やブラウザーへアクセスしません。')
    args = parser.parse_args(argv)
    if args.dry_run:
        print(json.dumps(dict(slot=due_slot(_now(), args.role), next=next_slots(_now()),
                              planning_only=True), ensure_ascii=False, indent=2))
        return 0
    if os.name != 'nt':
        print('実際の自動取得は、連携設定を保存した同じWindowsユーザーで実行してください。')
        return 2
    result = run_monthly(BASE, role=args.role, test=args.test, include_pdfs=args.include_pdfs,
                         progress=lambda message: print(message, flush=True))
    print(json.dumps({k: result.get(k) for k in ('status', 'exit_code', 'error', 'run_path', 'comparison_csv')},
                     ensure_ascii=False, indent=2))
    return result['exit_code']


if __name__ == '__main__':
    raise SystemExit(main())
