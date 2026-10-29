"""Compact dividend, tax refund and interest worksheet for the v3.6 report.

Only recorded receipts enter actual totals. Research dates and forecasts stay
separate; an unobserved price or forecast is never replaced with zero.
"""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import math
import unicodedata

from cost_basis import calculate
from custody_labels import event_label
from date_highlights import dates_in
from dividend_forecast import forecast
from market_data import load_research
from report_legacy import col, code, reporting_code
from security_names import load as load_names


JST = timezone(timedelta(hours=9))
SUMMARY_START = 11
SUMMARY_HEADERS = (
    'コード', '銘柄', '権利付き最終日', '配当金支払日', '本決算発表日',
    '株数', 'DPS', '配当利回り', '実績', '予想(税引後)', '年間予想',
    '累計配当金', '確認状態', '前月末日終値', '今月1日終値',
)
_INCOME = ('配当', '税還付', '税還', '利息')
_CLASS_ORDER = {'権利付き最終日': 0, '配当金支払日': 1, '本決算発表日': 2}


def _day(value):
    if isinstance(value, datetime):
        return value.astimezone(JST).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _number(value, optional=False):
    if value is None or value == '':
        if optional:
            return None
        raise ValueError('入金額を確認できません。元の記録を確認してください。')
    if isinstance(value, bool):
        raise ValueError('入金額・株数・単価は有限の数値で指定してください。')
    try:
        result = Decimal(str(value))
    except Exception:
        raise ValueError('入金額・株数・単価を確認できません。') from None
    if not result.is_finite() or not math.isfinite(float(result)):
        raise ValueError('入金額・株数・単価は有限数値で指定してください。')
    return result


def _short(value):
    return {'NISA(成)': 'N-成', 'NISA(積)': 'N-積', 'NISA（成）': 'N-成',
            'NISA（積）': 'N-積', '特定': '特', '税還付': '税還'}.get(value, value)


def _ident(event):
    return str(event.get('code') or '未分類')


def _dates(value):
    if isinstance(value, (date, datetime)):
        return [_day(value)]
    return dates_in(value or '')


def _price(fact, value_key, date_key, target, previous=False, known_through=None):
    """Use the explicitly dated observation; do not shift a missing first day."""
    try:
        observed = _day(fact.get(date_key))
        if known_through is not None and observed > _day(known_through):
            return None
        correct = ((observed.year, observed.month) == (target.year, target.month)
                   if previous else observed == target)
        if not correct:
            return None
        value = _number(fact.get(value_key), optional=True)
        return value if value is not None and value > 0 else None
    except (TypeError, ValueError):
        return None


def _forecast_amounts(store, activities, predicted, lines):
    """Retain the existing verified entitlement/custody policy for forecasts."""
    states = {}
    amounts = defaultdict(Decimal)
    for entry in lines:
        ident = str(entry['code'])
        if predicted.get(ident) is None:
            continue
        day = entry['quantity_date']
        if day not in states:
            states[day] = calculate(store, activities, day)[1]
        position = states[day].get((entry['account'], ident, entry['custody']))
        if position is None or not position['valid'] or position['quantity'] < 0:
            continue
        amounts[(ident, entry['payment'])] += (
            position['quantity'] * _number(entry['dps']) * _number(entry['net_rate']))
    return amounts


def _stock_forecast_eligible(ident, trades):
    """The legacy DPS rule is per domestic share, never per fund quote unit."""
    related = [row for row in trades if str(reporting_code(row)) == ident]
    if not related or ident.startswith('F-') or ident == '未分類':
        return False
    for row in related:
        category = row.get('asset_type') or '国内株式'
        currency = str(row.get('currency') or 'JPY').upper()
        quantity_unit = row.get('quantity_unit') or '株'
        price_unit = unicodedata.normalize('NFKC', str(row.get('price_unit') or '円/株')).replace(' ', '')
        if (category != '国内株式' or currency != 'JPY' or quantity_unit != '株'
                or price_unit != '円/株'):
            return False
    return True


def build_dividend_sheet(store, activities, as_of, today=None, trade_data=None):
    """Build one compact sheet; no ledger, settings or market data are written.

    ``trade_data`` may be a grouped Store adapter. Forecasts use it when supplied;
    receipt totals always come from the original durable activity records.
    """
    day = _day(as_of)
    today = _day(today) if today is not None else datetime.now(JST).date()
    first = day.replace(day=1)
    previous = first - timedelta(days=1)
    source = trade_data if hasattr(trade_data, 'records') else store
    trades = [row for row in source.records() if row['trade_date'] <= day.isoformat()]
    all_events = [row for row in activities.events() if row['date'] <= day.isoformat()]
    events = [row for row in all_events if row['type'] in _INCOME]
    if len(events) > 9998:
        raise ValueError('配当・入金明細が9998行を超えます。式範囲を拡張してください。')
    from trade_report import _account_labels, build_trade_rows
    labels = _account_labels(source.accounts())
    names = {str(reporting_code(row)): row.get('name', '') for row in trades}
    names.update({_ident(row): row.get('name', '') for row in events if row['type'] == '配当'})
    if '未分類' in names and not names['未分類']:
        names['未分類'] = 'コード未登録の配当'
    overrides = load_names(store.directory)
    names = {ident: overrides.get(ident, name) for ident, name in names.items()}
    facts = load_research(store.directory, day.year).get('securities', {})
    eligible = {ident for ident in names if _stock_forecast_eligible(ident, trades)}
    # The legacy DPS rule multiplies shares by a per-share dividend. A numeric
    # fund code does not establish that unit, just as an internal F-ID does not.
    # Filter before calculation so unsupported quantities never enter that rule.
    predicted, forecast_lines = forecast(source, activities,
                                         {ident: fact for ident, fact in facts.items() if ident in eligible},
                                         day.isoformat())
    for ident in names:
        if ident not in eligible:
            predicted[ident] = None
    forecast_amounts = _forecast_amounts(source, activities, predicted, forecast_lines)

    sheet = dict(name='配当', cells=[], widths={}, merges=[], filter=None, chart=None,
                 row_height=24, header_height=44.1, conditional_formats=[])
    cells = sheet['cells']

    def put(row, column, value=None, kind=None, **style):
        if isinstance(value, Decimal):
            value = float(value)
        cell = dict(r=row, c=column, v=value,
                    k=kind or ('number' if isinstance(value, (int, float)) else 'text'), fill=None)
        cell.update(style)
        cells.append(cell)
        return cell

    def formula(row, column, expression, kind='number', **style):
        return put(row, column, expression, 'formula_' + kind, **style)

    for column, title in enumerate(('日付', '口座', '銘柄', 'コード', '預り', '分類',
                                     '数量', '単価（円）', '受取額（円）'), 1):
        put(1, column, title)
    actual = defaultdict(Decimal)
    cumulative = defaultdict(Decimal)
    actual_payments = defaultdict(Decimal)
    for row, entry in enumerate(sorted(events, key=lambda value: (
            value['date'], value.get('account', ''), _ident(value), value['type']), reverse=True), 2):
        if entry.get('currency', 'JPY') != 'JPY':
            raise ValueError('配当・入金の通貨が円ではありません。円額へ推定換算せず原本を確認してください。')
        amount = _number(entry.get('net', entry.get('amount')))
        is_dividend = entry['type'] == '配当'
        values = [entry['date'], labels.get(entry['account'], entry.get('account_label', entry['account'])),
                  names.get(_ident(entry), entry.get('name', '')) if is_dividend else None,
                  code(_ident(entry)) if is_dividend else None,
                  _short(event_label(entry, trades)) if is_dividend else None,
                  _short(entry['type']),
                  _number(entry.get('quantity'), optional=True) if is_dividend else None,
                  _number(entry.get('per_share'), optional=True) if is_dividend else None,
                  amount]
        for column, value in enumerate(values, 1):
            put(row, column, value, 'date' if column == 1 else 'code' if column == 4 else None)
        if is_dividend:
            ident = _ident(entry)
            cumulative[ident] += amount
            if entry['date'][:4] == str(day.year):
                actual[ident] += amount
            actual_payments[(ident, entry['date'])] += amount

    end_detail = max(2, len(events) + 1)
    position_warnings = {str(reporting_code(row))
                         for row in build_trade_rows(trades, all_events, day.isoformat())
                         if not row['basis_known']}
    for column, title in enumerate(SUMMARY_HEADERS, SUMMARY_START):
        put(1, column, title)
    ordered = sorted(names, key=lambda ident: (
        -(actual[ident] + Decimal(str(predicted.get(ident) or 0))), -cumulative[ident], ident))
    for row, ident in enumerate(ordered, 2):
        fact = facts.get(ident, {})
        put(row, 11, code(ident), 'code')
        put(row, 12, names[ident])
        for column, key in ((13, 'rights'), (14, 'payments'), (15, 'earnings')):
            put(row, column, fact.get(key) or None)
        buys = f'SUMIFS(取引!$G$2:$G$10000,取引!$D$2:$D$10000,K{row},取引!$F$2:$F$10000,"B")'
        sells = f'SUMIFS(取引!$G$2:$G$10000,取引!$D$2:$D$10000,K{row},取引!$F$2:$F$10000,"S")'
        if ident in position_warnings or not any(str(reporting_code(trade)) == ident for trade in trades):
            put(row, 16, None, 'number')
        else:
            formula(row, 16, f'IF({buys}-{sells}<0,"",{buys}-{sells})')
        put(row, 17, _number(fact.get('dps'), optional=True))
        formula(row, 18, f'IF(AND(ISNUMBER(Q{row}),ISNUMBER(X{row}),X{row}>0),Q{row}/X{row},"")', 'percent')
        criteria = f'$I$2:$I${end_detail},$D$2:$D${end_detail},K{row},$F$2:$F${end_detail},"配当"'
        formula(row, 19, f'SUMIFS({criteria},$A$2:$A${end_detail},">="&DATE({day.year},1,1),$A$2:$A${end_detail},"<"&DATE({day.year + 1},1,1))')
        put(row, 20, _number(predicted.get(ident), optional=True))
        formula(row, 21, f'IF(ISNUMBER(T{row}),SUM(S{row}:T{row}),"")')
        formula(row, 22, f'SUMIFS({criteria})')
        notes = [str(fact.get('status') or '予定・株価の未確認項目は空欄')]
        prev_price = _price(fact, 'close', 'close_date', previous, previous=True, known_through=today)
        first_price = _price(fact, 'month_start_close', 'month_start_close_date', first, known_through=today)
        if prev_price is None:
            notes.append('前月末終値未確認')
        if first_price is None:
            notes.append('今月1日終値未確認')
        put(row, 23, '／'.join(notes))
        put(row, 24, prev_price, 'number')
        put(row, 25, first_price, 'number')

    summary_last = max(3, len(ordered) + 2)
    put(summary_last, 12, '合計')
    last_security = max(2, len(ordered) + 1)
    for column in (19, 22):
        letter = col(column)
        formula(summary_last, column, f'SUM({letter}2:{letter}{last_security})')
    for column in (20, 21):
        letter = col(column)
        formula(summary_last, column,
                f'IF(COUNT({letter}2:{letter}{last_security})={len(ordered)},SUM({letter}2:{letter}{last_security}),"")')

    cumulative_header = summary_last + 3
    for column, title in enumerate(('年', '配当', '税還', '利息'), 11):
        put(cumulative_header, column, title)
    years = range(min([day.year] + [int(entry['date'][:4]) for entry in events]), day.year + 1)
    annual_first = cumulative_header + 1
    annual_last = cumulative_header
    for row, year in enumerate(years, annual_first):
        annual_last = row
        put(row, 11, year, 'number', format='0"年"')
        for column, kind in ((12, '配当'), (13, '税還'), (14, '利息')):
            formula(row, column,
                f'SUMIFS($I$2:$I${end_detail},$F$2:$F${end_detail},"{kind}",'
                f'$A$2:$A${end_detail},">="&DATE(K{row},1,1),'
                f'$A$2:$A${end_detail},"<"&DATE(K{row}+1,1,1))')
    total_row = annual_last + 1
    put(total_row, 11, '累計')
    for column in (12, 13, 14):
        letter = col(column)
        formula(total_row, column, f'SUM({letter}{annual_first}:{letter}{annual_last})')
    grand_total = total_row + 1
    put(grand_total, 11, '総計')
    formula(grand_total, 12, f'SUM(L{total_row}:N{total_row})')

    calendar_label = grand_total + 3
    put(calendar_label, 11, 'カレンダー')
    calendar = {}
    for ident in ordered:
        fact = facts.get(ident, {})
        for key, kind in (('rights', '権利付き最終日'), ('payments', '配当金支払日'), ('earnings', '本決算発表日')):
            for stamp in _dates(fact.get(key)):
                calendar[(stamp, ident, kind)] = None
        for entry in fact.get('events', []):
            if not isinstance(entry, dict):
                continue
            for key, kind in (('rights', '権利付き最終日'), ('payment', '配当金支払日')):
                for stamp in _dates(entry.get(key)):
                    calendar[(stamp, ident, kind)] = None
    for (ident, stamp), amount in actual_payments.items():
        calendar[(_day(stamp), ident, '配当金支払日')] = None
    calendar_rows = sorted(calendar, key=lambda item: (item[0], _CLASS_ORDER[item[2]], item[1]))
    if len(calendar_rows) > 9998:
        raise ValueError('カレンダーが9998行を超えます。出力範囲を確認してください。')
    highlight = None
    for row, (stamp, ident, kind) in enumerate(calendar_rows, calendar_label + 1):
        put(row, 11, stamp.isoformat(), 'date')
        put(row, 12, names[ident])
        put(row, 13, kind)
        amount = None
        style = '#,##0'
        if kind == '配当金支払日':
            key = (ident, stamp.isoformat())
            if key in actual_payments:
                formula(row, 14,
                    f'SUMIFS($I$2:$I${end_detail},$D$2:$D${end_detail},{code(ident) if ident.isdigit() else chr(34)+ident.replace(chr(34),chr(34)*2)+chr(34)},'
                    f'$F$2:$F${end_detail},"配当",$A$2:$A${end_detail},K{row})',
                    format='#,##0"（実績）"')
            elif stamp > day and stamp.year == day.year and predicted.get(ident) is not None and any(
                    entry.get('payment') == stamp.isoformat() and entry.get('payment_year') in (None, day.year)
                    for entry in facts.get(ident, {}).get('events', []) if isinstance(entry, dict)):
                amount = forecast_amounts.get(key, Decimal(0))
                style = '#,##0"（予想）"'
                put(row, 14, amount, 'number', format=style)
        if highlight is None and stamp >= today:
            highlight = row
    if calendar_rows:
        start_row = calendar_label + 1
        end_row = calendar_label + len(calendar_rows)
        sheet['conditional_formats'].append(dict(
            range=f'K{start_row}:N{end_row}', dxf=2,
            formula=f'AND(ISNUMBER($K{start_row}),$K{start_row}>=TODAY(),COUNTIF($K${start_row}:$K{start_row},">="&TODAY())=1)'))

    widths = {1: 13, 2: 12, 3: 25, 4: 10, 5: 12, 6: 10, 7: 12, 8: 15, 9: 18,
              10: 3, 11: 13, 12: 25, 13: 22, 14: 22, 15: 22, 16: 12, 17: 12,
              18: 13, 19: 17, 20: 17, 21: 17, 22: 17, 23: 38, 24: 16, 25: 16}
    sheet['widths'] = {str(key): value for key, value in widths.items()}
    for cell in cells:
        cell['align'] = {'v': 'center', 'wrap': True}
        if cell['r'] == 1 or cell['c'] in (2, 4, 5, 6, 11, 12, 13):
            cell['align']['h'] = 'center'
    sheet['blocks'] = dict(summary_last_row=summary_last, cumulative_header_row=cumulative_header,
                           cumulative_total_row=total_row, cumulative_last_row=grand_total,
                           calendar_label_row=calendar_label, calendar_first_row=calendar_label + 1,
                           calendar_last_row=calendar_label + len(calendar_rows),
                           calendar_highlight_row=highlight)
    return sheet
