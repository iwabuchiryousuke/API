"""Read only cash and current NISA allowances from the verified SBI session.

This reader never visits My資産, historical views, a portfolio, or an invented
endpoint. Explicitly displayed native USD remains distinct from yen valuation.
Unknown fields are omitted; previously captured values are never substituted.
"""
from decimal import Decimal
import json
import time

from ledger import LedgerError
from login_credentials import CredentialError
from sbi_views import ObservationConflict

REQUIRED = ('cash_total', 'usd_cash', 'growth_remaining', 'tsumitate_remaining')
SUMMARY = ('cash_total', 'broker_cash', 'bank', 'growth_remaining', 'tsumitate_remaining')
LABELS = {'cash_total': '円現金', 'usd_cash': '米ドル現金',
          'growth_remaining': 'NISA成長枠', 'tsumitate_remaining': 'NISAつみたて枠'}

_VISIBLE_TABLES = r'''els=>{
 const visible=e=>Array.from(e.getClientRects()).some(r=>r.width>0&&r.height>0)&&getComputedStyle(e).visibility==='visible';
 return els.filter(visible).map(t=>Array.from(t.rows).filter(visible).map(r=>Array.from(r.cells).filter(visible).map(c=>c.innerText)));
}'''
_VISIBLE_GRIDS = r'''els=>{
 const visible=e=>Array.from(e.getClientRects()).some(r=>r.width>0&&r.height>0)&&getComputedStyle(e).visibility==='visible';
 return els.filter(visible).map(t=>Array.from(t.querySelectorAll('[role=row]')).filter(visible).map(r=>Array.from(r.querySelectorAll('[role=columnheader],[role=rowheader],[role=cell],[role=gridcell]')).filter(visible).map(c=>c.innerText)));
}'''


def _snapshot(page):
    from auto_login import trusted
    from sbi_views import frames
    result = []
    for frame in frames(page):
        if not trusted(frame):
            continue
        try:
            embedded = frame
            visible = True
            while getattr(embedded, 'parent_frame', None) is not None:
                if not embedded.frame_element().is_visible():
                    visible = False
                    break
                embedded = embedded.parent_frame
            if not visible:
                continue
            text = frame.locator('body').inner_text(timeout=3000)
            tables = frame.locator('table').evaluate_all(_VISIBLE_TABLES)
            tables += frame.locator('[role="table"],[role="grid"]').evaluate_all(_VISIBLE_GRIDS)
            result.append((text, tables))
        except Exception:
            # A detached frame must be read again during the bounded wait.
            continue
    if not result:
        raise LedgerError('現在の口座画面の本文を読み取れません。')
    return result


def _values(page, kind):
    from sbi_views import parse_summary, parse_currency_summary, _jst_now, norm
    result = {}
    texts = []
    for text, tables in _snapshot(page):
        texts.append(text)
        try:
            if kind == '口座サマリー':
                parsed = parse_summary(text, tables)
                parsed = {key: value for key, value in parsed.items() if key in SUMMARY}
                for key in ('growth_remaining', 'tsumitate_remaining'):
                    if key in parsed and parsed[key]['year'] != _jst_now().year:
                        parsed.pop(key)
            else:
                # Responsive currency tables can spell out the cash header.
                # Only exact cash synonyms are normalized, never 評価額/余力.
                cash_headers={'現金残高','外貨現金残高','外貨預り金残高','預り金(外貨)'}
                normalized=[]
                for table in tables:
                    normalized.append([
                        ['預り金' if norm(cell) in cash_headers else cell for cell in row]
                        for row in table])
                currency_text=text.replace('米ドル現金残高','米ドル残高').replace('米ドル預り金残高','米ドル残高')
                parsed = parse_currency_summary(currency_text, normalized, required=False)
                parsed = {'usd_cash': parsed['usd_cash']} if 'usd_cash' in parsed else {}
        except ObservationConflict:
            raise
        except LedgerError:
            parsed = {}
        for key, value in parsed.items():
            if key in result and result[key] != value:
                raise ObservationConflict('現在の口座画面に異なる残高・NISA枠が表示されています。')
            result[key] = value
    if all(key in result for key in ('cash_total', 'broker_cash', 'bank')):
        if Decimal(str(result['cash_total'])) != Decimal(str(result['broker_cash'])) + Decimal(str(result['bank'])):
            raise ObservationConflict('円現金合計と証券・銀行の内訳が一致しません。')
    return '\n'.join(texts), result


def read_current(page, kind, *, owner=None, cancelled=lambda: False,
                 timeout=7, stable_seconds=.6):
    """Wait for the requested current fields, including delayed NISA/USD rows."""
    from sbi_asset_runtime import current_view_busy
    from sbi_views import _check_asset_identity, _check_cancelled
    if kind not in ('口座サマリー', '口座(外貨建)'):
        raise ValueError('現在取得の画面種別を確認してください。')
    required = ('cash_total', 'growth_remaining', 'tsumitate_remaining') if kind == '口座サマリー' else ('usd_cash',)
    deadline = time.monotonic() + timeout
    previous = None
    stable_since = None
    best = None
    last_error = None
    while time.monotonic() < deadline:
        _check_cancelled(cancelled)
        _check_asset_identity(page, owner)
        if current_view_busy(page):
            previous = None; stable_since = None; best = None
            page.wait_for_timeout(200)
            continue
        try:
            text, values = _values(page, kind)
            signature = json.dumps(values, sort_keys=True, ensure_ascii=False)
            if signature != previous:
                previous = signature; stable_since = time.monotonic(); best = None
            elif values and time.monotonic() - stable_since >= stable_seconds:
                best = (text, values)
                if all(values.get(key) is not None for key in required):
                    return best
        except LedgerError as error:
            last_error = error; previous = None; stable_since = None; best = None
        page.wait_for_timeout(200)
    _check_cancelled(cancelled)
    _check_asset_identity(page, owner)
    if best is not None:
        return best
    if last_error is not None:
        raise last_error
    return '', {}


def _optional_navigation(page, names, owner, cancelled):
    from sbi_views import navigate, _check_asset_identity, _check_cancelled
    try:
        page = navigate(page, names, timeout=5, cancelled=cancelled)
    except CredentialError:
        # A missing/ambiguous menu is not approval of this page. Only the
        # explicit balance labels below can establish a captured value.
        _check_cancelled(cancelled)
    _check_asset_identity(page, owner)
    return page


def _has_values(page, kind):
    try:
        return bool(_values(page, kind)[1])
    except ObservationConflict:
        raise
    except LedgerError:
        return False


def _show_view(page, kind, owner, cancelled):
    if _has_values(page, kind):
        return page
    page = _optional_navigation(page, ['口座管理'], owner, cancelled)
    names = (['口座(円建)'], ['口座サマリー']) if kind == '口座サマリー' else (['口座(外貨建)'],)
    for target in names:
        if _has_values(page, kind):
            break
        page = _optional_navigation(page, target, owner, cancelled)
    return page


def _reveal_currency_rows(page, owner, cancelled):
    from auto_login import trusted
    from sbi_views import frames, _check_asset_identity, _check_cancelled
    changed=False
    for frame in frames(page):
        if not trusted(frame):continue
        for name in ('残高ゼロの通貨を表示','残高0の通貨を表示','残高のない通貨を表示',
                     '残高ゼロの項目を表示','残高0の項目を表示'):
            choices=frame.get_by_role('checkbox',name=name,exact=True)
            visible=[choices.nth(index) for index in range(choices.count()) if choices.nth(index).is_visible()]
            if len(visible)>1:raise LedgerError('外貨残高の表示切替を一意に確認できません。')
            if visible and not visible[0].is_checked():
                _check_cancelled(cancelled);_check_asset_identity(page,owner)
                visible[0].check(timeout=5000);changed=True
    if changed:page.wait_for_timeout(200)
    _check_cancelled(cancelled);_check_asset_identity(page,owner)


def capture_current(page, store, account, *, owner=None, identity_verified=False,
                    progress=lambda _: None, cancelled=lambda: False):
    """Capture this run's JPY cash, USD cash and NISA; never restore past days."""
    from sbi_views import (select_asset_page, save_observation, _jst_now,
                           _check_asset_identity, _check_cancelled)
    if account not in ('A', 'B', 'C', 'D'):
        raise LedgerError('現在残高の自動取得はA～Dです。')
    _check_cancelled(cancelled)
    page = select_asset_page(page, owner=owner, identity_verified=identity_verified,
                             progress=progress, cancelled=cancelled)
    _check_asset_identity(page, owner, require_match=bool(owner) and not identity_verified)
    captured = {}
    stamps = {}
    for kind in ('口座サマリー', '口座(外貨建)'):
        _check_cancelled(cancelled)
        page = _show_view(page, kind, owner, cancelled)
        if kind=='口座(外貨建)':
            try:_reveal_currency_rows(page,owner,cancelled)
            except LedgerError:
                # Ambiguous optional display controls are not clicked. The
                # already visible, explicitly labelled balances can still read.
                progress(f'口座{account}：外貨の表示切替を確認できないため、現在表示されている残高だけを確認します。')
        try:
            text, values = read_current(page, kind, owner=owner, cancelled=cancelled)
        except ObservationConflict:
            # Contradictory money is not a missing optional field.
            raise
        except LedgerError:
            values = {}; text = ''
        _check_cancelled(cancelled)
        _check_asset_identity(page, owner)
        if values:
            payload = save_observation(store, account, kind, text, values)
            captured.update(values)
            for key in values:
                stamps[key] = payload['observed_at']
            progress(f'口座{account}：{kind}の現在残高を確認しました。')
        else:
            progress(f'口座{account}：{kind}の現在残高は未取得です。別の取得値で補いません。')
    missing = [key for key in REQUIRED if captured.get(key) is None]
    available = [key for key in REQUIRED if key not in missing]
    if missing:
        progress(f'口座{account}：現在値の未取得＝' + '、'.join(LABELS[key] for key in missing))
    return dict(complete=not missing, available=available, missing=missing,
                usd_cash_observed='usd_cash' in captured, values=captured,
                observed_at=max(stamps.values()) if stamps else _jst_now().isoformat(),
                component_observed_at=stamps)
