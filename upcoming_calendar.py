"""Read-only, shared calendar projection for the workbook and the main window.

Current holdings determine membership. Recorded income remains in the ledger
and its cumulative totals even when a security has since been sold completely.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unicodedata

from date_highlights import months_after
from market_data import load_research
from report_legacy import reporting_code
from security_names import load as load_names
from trade_report import GroupedStore, build_trade_rows


JST = timezone(timedelta(hours=9))
_CLASS_ORDER = {'権利付き最終日': 0, '配当金支払日': 1, '本決算発表日': 2}


def _code(value):
    return unicodedata.normalize('NFKC', str(value or '未分類')).strip().upper()


def _holdings(trades, events, today):
    """Return known totals or None; uncertain history is never a zero balance."""
    latest = {}
    for row in build_trade_rows(trades, events, today.isoformat()):
        key = (row['account'], _code(reporting_code(row)), row['custody'])
        latest.setdefault(key, row)
    grouped = defaultdict(list)
    for (_, ident, _), row in latest.items():
        grouped[ident].append(row)
    return {
        ident: (sum((row['calculated_quantity'] for row in rows), Decimal(0))
                if all(row['basis_known'] and row['calculated_quantity'] >= 0 for row in rows)
                else None)
        for ident, rows in grouped.items()
    }


def schedule_rows(store, activities, today=None, upcoming_only=False, *, as_of=None, trade_data=None):
    """Project current-year dates without fetching or modifying any information.

    ``amount`` is numeric or None. ``amount_kind`` is private provenance, never a
    display suffix. The optional report cutoff changes receipt/forecast amounts,
    while calendar membership always uses holdings as of today's JST date.
    Unknown holdings stay visible and cannot produce an estimated amount.
    """
    # These existing financial policies also serve the security summary. The
    # import is intentionally local: the sheet calls this projection at runtime.
    from dividend_report import (_calendar_dates, _day, _forecast_amounts,
                                 _number, _stock_forecast_eligible)
    from dividend_forecast import forecast

    today = _day(today) if today is not None else datetime.now(JST).date()
    boundary = _day(as_of) if as_of is not None else today
    source = trade_data if hasattr(trade_data, 'records') else GroupedStore(store)
    # GroupedStore supplies canonical identifiers and custody buckets. A custom
    # raw adapter is normalized by the same read-only grouping operation too.
    if not isinstance(source, GroupedStore):
        source = GroupedStore(source)
    records = source.records()
    all_events = activities.events()
    current_trades = [row for row in records if row['trade_date'] <= today.isoformat()]
    current_events = [row for row in all_events if row['date'] <= today.isoformat()]
    holdings = _holdings(current_trades, current_events, today)
    names = {_code(reporting_code(row)): row.get('name', '') for row in current_trades}
    names.update({_code(row.get('code')): row.get('name', '')
                  for row in current_events if row['type'] in ('配当', '株式分割')})
    if '未分類' in names and not names['未分類']:
        names['未分類'] = 'コード未登録の配当'
    overrides = {_code(ident): name for ident, name in load_names(store.directory).items()}
    names = {ident: overrides.get(ident, name) for ident, name in names.items()
             if holdings.get(ident) != 0}
    if not names:
        return []

    facts = load_research(store.directory, today.year).get('securities', {})
    financial_facts = (facts if boundary.year == today.year else
                       load_research(store.directory, boundary.year).get('securities', {}))
    financial_trades = [row for row in records if row['trade_date'] <= boundary.isoformat()]
    eligible = {ident for ident in names if holdings.get(ident) is not None
                and _stock_forecast_eligible(ident, financial_trades)}
    predicted, lines = forecast(source, activities,
                                {ident: fact for ident, fact in financial_facts.items() if ident in eligible},
                                boundary.isoformat())
    amounts = _forecast_amounts(source, activities, predicted, lines)
    actual = defaultdict(Decimal)
    for event in all_events:
        ident = _code(event.get('code'))
        if (event['type'] == '配当' and ident in names
                and event['date'] <= boundary.isoformat()
                and _day(event['date']).year == today.year):
            if event.get('currency', 'JPY') != 'JPY':
                raise ValueError('配当の通貨が円ではありません。円額へ推定換算せず原本を確認してください。')
            actual[(ident, event['date'])] += _number(event.get('net', event.get('amount')))

    dates = set()
    for ident in names:
        fact = facts.get(ident, {})
        for key, kind in (('rights', '権利付き最終日'), ('payments', '配当金支払日'), ('earnings', '本決算発表日')):
            dates.update((stamp, ident, kind) for stamp in _calendar_dates(fact, key, today.year))
    dates.update((_day(stamp), ident, '配当金支払日') for ident, stamp in actual)
    if len(dates) > 9998:
        raise ValueError('カレンダーが9998行を超えます。出力範囲を確認してください。')
    result = []
    until = months_after(today, 1)
    for stamp, ident, kind in sorted(dates, key=lambda item: (item[0], _CLASS_ORDER[item[2]], item[1])):
        if upcoming_only and stamp < today:
            continue
        amount = None; amount_kind = None
        key = (ident, stamp.isoformat())
        if kind == '配当金支払日':
            if key in actual:
                amount = actual[key]; amount_kind = '実績'
            elif (stamp > boundary and stamp.year == boundary.year and ident in eligible
                  and predicted.get(ident) is not None and any(
                      event.get('payment') == stamp.isoformat()
                      and event.get('payment_year') in (None, boundary.year)
                      for event in financial_facts.get(ident, {}).get('events', []) if isinstance(event, dict))):
                amount = amounts.get(key, Decimal(0)); amount_kind = '予想'
        result.append(dict(date=stamp.isoformat(), code=ident, name=names[ident],
                           kind=kind, type=kind, amount=amount, amount_kind=amount_kind,
                           within_month=today <= stamp <= until,
                           held_quantity=holdings.get(ident)))
    return result
