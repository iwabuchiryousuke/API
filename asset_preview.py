"""Read-only views of committed assets and validated, unapplied captures.

Journal replay happens in a disposable clone. Neither a failed PDF import nor
opening this view commits captured values to the user's transaction ledger.
"""
from datetime import date,datetime,timedelta,timezone
import json
import math
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from acquisition_journal import JournalError,replay_captures
from ledger import LedgerError,Store


JST=timezone(timedelta(hours=9))
_JOURNAL_WARNING=('未反映の取得証跡を確認できないため、登録済みの資産だけを表示しています。'
                  '取得証跡と原本は変更していません。data/acquisitionsを確認してください。')
_RECORD_WARNING=('取得時刻・日付・保存内容を確認できない資産記録があります。'
                 '確認できる記録だけを表示し、不明な時刻を現在の取得とは扱いません。')
_AMOUNTS={'domestic','foreign','cash','foreign_cash','bank','fund','bond',
          'usd_fund','usd_bond','usd_cash','cash_total','broker_cash','summary_stocks','summary_total'}


def _warn(warnings,message):
    if message not in warnings:warnings.append(message)


def _day(value):
    if not isinstance(value,str) or date.fromisoformat(value).isoformat()!=value:
        raise ValueError('表示基準日はYYYY-MM-DDで指定してください。')
    return value


def _stamp(value):
    result=datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:raise ValueError('timezone required')
    return result.astimezone(JST)


def _exists(store,name):
    return store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone() is not None


def _number(value):
    try:
        if value is not None and (isinstance(value,bool) or not isinstance(value,(int,float))
                                  or not math.isfinite(value) or value<0):raise ValueError()
    except OverflowError:raise ValueError() from None


def _validate_values(values):
    if not isinstance(values,dict):raise ValueError()
    for key in _AMOUNTS&values.keys():_number(values[key])
    for key in ('growth_remaining','tsumitate_remaining'):
        if key not in values:continue
        limit=values[key]
        if not isinstance(limit,dict) or type(limit.get('year')) is not int or not 1900<=limit['year']<=9999:raise ValueError()
        _number(limit.get('remaining'))


def _read_assets(store,cutoff,warnings):
    history=[];snapshots={}
    if _exists(store,'account_observations'):
        for row in store.db.execute('SELECT account,kind,observed_at,payload FROM account_observations'):
            try:
                observed=_stamp(row['observed_at']);payload=json.loads(row['payload'])
                if not isinstance(payload,dict):raise ValueError()
                _validate_values(payload.get('values'))
                if _stamp(payload.get('observed_at'))!=observed or payload.get('source')!=row['kind']:raise ValueError()
            except (ValueError,TypeError,OverflowError):
                _warn(warnings,_RECORD_WARNING);continue
            if observed.date().isoformat()>cutoff:continue
            history.append({'account':row['account'],'kind':row['kind'],
                            'observed_at':row['observed_at'],'payload':payload})
    history.sort(key=lambda item:(_stamp(item['observed_at']),item['account'],item['kind']))
    observations={};seen={}
    for item in history:
        key=(item['account'],item['kind'])
        stamp=_stamp(item['observed_at']);previous=seen.get(key)
        if previous is not None and previous[0]==stamp and previous[1]['values']!=item['payload']['values']:
            observations.pop(key,None)
            _warn(warnings,'同じ取得時刻の資産額が一致しない記録があります。現在資産の候補を一意に決めず、履歴と原本の確認を優先してください。')
            continue
        if previous is not None and previous[0]==stamp and key not in observations:continue
        seen[key]=(stamp,item['payload'])
        observations[key]=item['payload']
    if _exists(store,'asset_snapshots'):
        for row in store.db.execute('SELECT account,as_of,payload FROM asset_snapshots'):
            try:
                day=_day(row['as_of']);payload=json.loads(row['payload'])
                if not isinstance(payload,dict):raise ValueError()
                for key in ('domestic','foreign','cash','foreign_cash','growth_remaining','tsumitate_remaining'):
                    _number(payload.get(key))
            except (ValueError,TypeError):
                _warn(warnings,_RECORD_WARNING);continue
            if day>cutoff:continue
            # Preserve readable dated/manual values even if a legacy capture
            # timestamp cannot establish freshness. Never invent its timezone.
            try:_stamp(payload.get('recorded_at'))
            except (ValueError,TypeError,OverflowError):_warn(warnings,_RECORD_WARNING)
            snapshots[(row['account'],day)]=payload
    return observations,history,snapshots


def _boundaries(stage,targets,windows,warnings):
    if not targets:return {}
    try:
        from asset_history import boundary_candidates
    except ImportError:
        _warn(warnings,'指定日の当日実測候補を表示する機能を読み込めません。現在の記録は表示しています。')
        return {}
    result={}
    for account in 'ABCD':
        try:
            result[account]=boundary_candidates(stage,account,targets,acquisition_windows=windows)
        except (LedgerError,ValueError,TypeError,sqlite3.Error):
            result[account]={}
            _warn(warnings,'口座'+account+'の当日実測候補を確認できません。確認できた現在の記録は表示しています。')
    return result


def _pending(observations,snapshots,committed,committed_snapshots):
    accounts=set()
    for key,payload in observations.items():
        previous=committed.get(key)
        if previous is None or _stamp(payload['observed_at'])>_stamp(previous['observed_at']):
            accounts.add(key[0])
    for key,payload in snapshots.items():
        if payload!=committed_snapshots.get(key):accounts.add(key[0])
    return sorted(accounts)


def preview_assets(base,live,cutoff,target_dates=()):
    """Preview current/history values without changing live data or schemas.

    cutoff and target_dates are JST calendar dates. ``pending_accounts`` marks
    changed visible values, not every account that has an old journal. Windows
    come only from a successful, complete validation/replay of all journals.
    """
    cutoff=_day(cutoff)
    requested=tuple(dict.fromkeys(_day(day) for day in target_dates))
    targets=tuple(day for day in requested if day<=cutoff)
    warnings=[]
    if len(targets)<len(requested):
        _warn(warnings,'表示基準日より後の指定日は、資産の確認候補を表示しません。')
    result={'observations':{},'committed_observations':{},
            'pending_accounts':[],'warnings':warnings,'snapshots':{},
            'windows':[],'history_observations':[],'boundary_candidates':{}}
    # SQLite backup cannot progress on this connection's uncommitted write.
    # The caller normally opens the viewer only after application work finishes.
    if live.db.in_transaction:
        _warn(warnings,'未確定の台帳操作があるため、取得証跡のプレビューは行っていません。処理の終了後に開き直してください。')
        return result
    committed,committed_history,committed_snapshots=_read_assets(live,cutoff,warnings)
    result.update(observations=committed,committed_observations=committed,
                  snapshots=committed_snapshots,history_observations=committed_history)
    try:
        with TemporaryDirectory(prefix='sbi_asset_preview_') as directory:
            stage=Store(Path(directory)/'data')
            try:
                live.db.backup(stage.db)
                committed,committed_history,committed_snapshots=_read_assets(stage,cutoff,warnings)
                result.update(observations=committed,committed_observations=committed,
                              snapshots=committed_snapshots,history_observations=committed_history)
                try:
                    replayed=replay_captures(Path(base),stage)
                except (JournalError,OSError,sqlite3.Error):
                    # Even a replacement replay implementation that mutates and
                    # then raises must not expose a partial preview.
                    stage.db.rollback();live.db.backup(stage.db)
                    _warn(warnings,_JOURNAL_WARNING)
                    result['boundary_candidates']=_boundaries(stage,targets,(),warnings)
                    return result
                observations,history,snapshots=_read_assets(stage,cutoff,warnings)
                windows=replayed.get('windows',[])
                for warning in replayed.get('warnings',[]):
                    if warning.startswith('MANUAL_ASSET_PRESERVED:'):
                        _,account,day=warning.split(':',2)
                        warning='口座'+account+'・'+day+'の手動資産記録を保持しました。取得証跡では上書きしていません。'
                    _warn(warnings,warning)
                result.update(observations=observations,snapshots=snapshots,
                    pending_accounts=_pending(observations,snapshots,committed,committed_snapshots),
                    windows=windows,history_observations=history,
                    boundary_candidates=_boundaries(stage,targets,windows,warnings))
                return result
            finally:stage.close()
    except (OSError,sqlite3.Error):
        _warn(warnings,_JOURNAL_WARNING)
        return result
