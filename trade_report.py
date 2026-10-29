"""Daily trade presentation and formula-based running costs for v3.6.

Raw ledger rows, duplicate signatures and PDF evidence are never rewritten.
One buy/sell bucket is formed before the once-per-day acquisition-price
rounding. Stocks and fund quote units are retained separately.
"""
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, localcontext
import hashlib
import json
import unicodedata

from cost_basis import instrument_id
from custody_labels import label, trade_label
from ledger import LedgerError, parse_date
from report_legacy import HEADERS, NUM, Sheet, code, reporting_code


JST = timezone(timedelta(hours=9))
GAIN = '[Red]+#,##0;[Blue]-#,##0;0'
MAX_ROWS = 9999
_FIELDS = ('quantity', 'settlement_amount', 'fees', 'tax')


def _decimal(value, *, positive=False):
    try:
        if isinstance(value, bool):raise ValueError()
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or positive and result <= 0:raise ValueError()
        return result
    except (InvalidOperation, ValueError, TypeError):
        raise LedgerError('取引の数量・金額を非負の有限数値として確認できません。原本と台帳を保持しました。') from None


def _text(value):
    return format(value, 'f')


def _custody(record, records):
    value = trade_label(record, records)
    return _bucket_alias(value)


def _bucket_alias(value):
    return {'特': '特定', 'N-成': 'NISA(成)', 'N(成)': 'NISA(成)',
            'N-積': 'NISA(積)', 'N(積)': 'NISA(積)'}.get(value, value)


def short(value):
    return {'母(雅美)': '母', '父(慶太)': '父', '買': 'B', '売': 'S',
            '税還付': '税還', 'NISA(成)': 'N-成', 'NISA(積)': 'N-積',
            '特定': '特'}.get(value, value)


def _units(record):
    category = record.get('asset_type') or '国内株式'
    is_fund = category == '投資信託'
    quantity = record.get('quantity_unit') or ('口' if is_fund else '株')
    price = record.get('price_unit') or ('円/1万口' if is_fund else '円/株')
    currency = record.get('currency') or 'JPY'
    if not all(isinstance(value, str) and value for value in (category, quantity, price, currency)):
        raise LedgerError('取引の通貨・数量単位・価格単位の形式を確認できません。')
    currency = currency.upper()
    # Legacy sources used several spellings for the same quote convention.
    compact = unicodedata.normalize('NFKC', price).replace(' ', '').replace('10,000', '10000')
    if is_fund and compact in ('円/1万口', '円/10000口', '円/万口'):price = '円/1万口'
    elif is_fund and compact in ('円/口', '円/1口'):price = '円/口'
    elif not is_fund and compact in ('円/株', '円/1株'):price = '円/株'
    supported = ((category == '国内株式' and quantity == '株' and price == '円/株') or
                 (is_fund and quantity == '口' and price in ('円/口', '円/1万口')))
    if currency != 'JPY' or not supported:
        raise LedgerError('円建の国内株式・投資信託として通貨・数量単位・価格単位を確認できません。原本を保持し、円欄へ推定転記しません。')
    return category, quantity, price, currency


def _quote_unit(units):
    return 10000 if units[2] == '円/1万口' else 1


def group_trades(records):
    """Return daily buckets, retaining source-row IDs for a reversible view.

    The grouping key is the requested date/account/instrument/custody/side.
    Conflicting quote units or currencies cannot silently share that bucket.
    An empty security code falls back to its normalized instrument name.
    """
    originals = [dict(row, code=unicodedata.normalize('NFKC', str(row.get('code') or '')).strip().upper())
                 for row in records]
    buckets = {}
    with localcontext() as context:
        context.prec = 50
        for index, source in enumerate(originals):
            day = parse_date(str(source.get('trade_date', '')))
            side = {'B': '買', 'S': '売'}.get(source.get('side'), source.get('side'))
            if side not in ('買', '売'):
                raise LedgerError('売買区分を確認できません。分割は売買とは別の通知として扱います。')
            if not source.get('account'):
                raise LedgerError('取引の口座IDを確認できません。')
            current = dict(source, trade_date=day, side=side)
            custody = _custody(current, originals)
            key = (day, str(source['account']), instrument_id(current), custody, side)
            units = _units(current)
            quantity = _decimal(source.get('quantity'), positive=True)
            amounts = {field: _decimal(source.get(field, '0')) for field in _FIELDS if field != 'quantity'}
            if key not in buckets:
                row = dict(current, custody=custody)
                row.update({field: Decimal(0) for field in _FIELDS})
                row.update(source_record_ids=[], source_count=0, settlement_dates=[],
                           sources=[], quote_unit=_quote_unit(units),
                           _units=units)
                buckets[key] = row
            row = buckets[key]
            if row['_units'] != units:
                raise LedgerError('同日・同口座・同銘柄の通貨や数量・価格単位が一致しません。合算せず原本を確認してください。')
            row['quantity'] += quantity
            for field, amount in amounts.items():row[field] += amount
            row['source_count'] += 1
            row['source_record_ids'].append(str(source.get('record_id', f'input-{index}')))
            if source.get('settlement_date'):
                day_settled = parse_date(str(source['settlement_date']))
                if day_settled not in row['settlement_dates']:row['settlement_dates'].append(day_settled)
            if source.get('source') and source['source'] not in row['sources']:row['sources'].append(source['source'])
        output = []
        for key, row in buckets.items():
            row['price'] = row['settlement_amount'] / row['quantity'] * row['quote_unit']
            for field in _FIELDS + ('price',):row[field] = _text(row[field])
            category, quantity_unit, price_unit, currency = row.pop('_units')
            row.update(asset_type=category, quantity_unit=quantity_unit,
                       price_unit=price_unit, currency=currency)
            row['settlement_dates'].sort()
            row['settlement_date'] = row['settlement_dates'][0] if len(row['settlement_dates']) == 1 else ''
            row['source_record_ids'].sort()
            row['source'] = ' / '.join(sorted(row.pop('sources')))
            identity = hashlib.sha256(json.dumps(key, ensure_ascii=False).encode('utf-8')).hexdigest()[:24]
            row['record_id'] = row['group_id'] = 'daily-' + identity
            row.pop('signature', None)
            output.append(row)
    return sorted(output, key=lambda row: (row['trade_date'], row['account'],
                                           instrument_id(row), row['custody'], row['side']))


class GroupedStore:
    """Read-only adapter used by report/forecast consumers; never an importer."""
    def __init__(self, store):self._store = store
    def records(self, month=''):return group_trades(self._store.records(month))
    def __getattr__(self, name):return getattr(self._store, name)


def _state():
    return dict(quantity=Decimal(0), average=Decimal(0), valid=True, issue='', units=None)


def _rounded(value):
    return value.to_integral_value(rounding=ROUND_CEILING)


def build_trade_rows(records, events=(), as_of=None):
    """Return descending daily movements with independent expected arithmetic.

    Within one date: a split takes effect before buys, and daily buys before
    daily sales. The resulting rows put sales above buys in the descending
    workbook. This is a declared daily accounting order, not a claim that the
    intraday execution sequence was observed.
    """
    original = [dict(row) for row in records]
    boundary = parse_date(as_of) if as_of else '9999-12-31'
    trades = group_trades([row for row in original if row['trade_date'] <= boundary])
    cash_events = [dict(event, code=unicodedata.normalize('NFKC', str(event.get('code') or '')).strip().upper())
                   for event in events if event.get('date', '9999-12-31') <= boundary]
    timeline = [(row['trade_date'], 1 if row['side'] == '買' else 2,
                 row['account'], instrument_id(row), row['custody'], row) for row in trades]
    for event in cash_events:
        if event.get('type') == '株式分割':
            timeline.append((parse_date(event['date']), 0, event['account'],
                             str(event['code']), '', event))
    timeline.sort(key=lambda item: item[:5])
    first_trade = {}
    for row in trades:
        key = (row['account'], instrument_id(row))
        first_trade[key] = min(first_trade.get(key, row['trade_date']), row['trade_date'])
    missing_opening = set()
    for event in cash_events:
        key = (event.get('account'), str(event.get('code') or ''))
        if event.get('type') == '配当' and key in first_trade and event['date'] < first_trade[key]:
            missing_opening.add(key)
    positions = defaultdict(_state)
    output = []
    with localcontext() as context:
        context.prec = 50
        for day, priority, account, ident, bucket, source in timeline:
            if priority == 0:
                allocations = source.get('allocations')
                if allocations:
                    choices = list(allocations.items())
                else:
                    existing = [key[2] for key, state in positions.items()
                                if key[:2] == (account, ident) and state['quantity'] > 0]
                    choices = [(existing[0], [source.get('before', 0), source.get('added', 0), source.get('after', 0)])] if len(existing) == 1 else [
                        ('預り未確認', [source.get('before', 0), source.get('added', 0), source.get('after', 0)])]
                for custody, values in sorted(choices):
                    before, added, after = (_decimal(value) for value in values)
                    if not before and not added:continue
                    if after != before + added:
                        raise LedgerError('分割前・追加・分割後の株数が整合しません。')
                    raw_custody = ''.join(unicodedata.normalize('NFKC', str(custody)).split())
                    normalized = _bucket_alias(label(custody, day))
                    if raw_custody == 'NISA':
                        options = [key[2] for key, state in positions.items()
                                   if key[:2] == (account, ident) and 'NISA' in key[2] and state['quantity'] > 0]
                        normalized = options[0] if len(options) == 1 else 'NISA(要確認)'
                    state = positions[(account, ident, normalized)]
                    known = state['valid'] and state['quantity'] == before and normalized != '預り未確認'
                    old_quantity, old_average = state['quantity'], state['average']
                    state['quantity'] += added
                    if not known:
                        state['valid'] = False;state['issue'] = '分割前の数量・預り区分・取得履歴を確認できません。'
                    state['average'] = (_rounded(old_quantity * old_average / state['quantity'])
                                        if known and state['quantity'] else Decimal(0) if known else None)
                    row = dict(source, trade_date=day, custody=normalized, side='買',
                               quantity=_text(added), price='0', settlement_amount='0',
                               fees='0', tax='0', movement='split', quote_unit=1,
                               asset_type='国内株式', currency='JPY', quantity_unit='株', price_unit='円/株',
                               source_record_ids=(), source_count=1,
                               record_id='split-' + hashlib.sha256(json.dumps([account, ident, day, normalized], ensure_ascii=False).encode()).hexdigest()[:24])
                    row.update(basis_known=known, gain_known=False,
                               calculated_quantity=state['quantity'], calculated_average=state['average'],
                               calculated_gain=Decimal(0), issue=state['issue'])
                    output.append(row)
                continue
            row = dict(source, movement='trade')
            key = (account, ident, bucket);state = positions[key]
            units = _units(row)
            if state['units'] is not None and state['units'] != units:
                raise LedgerError('同じ口座・銘柄・預り区分の通貨または価格単位が期間中に変わっています。')
            state['units'] = units
            if units[0] not in ('国内株式', '投資信託') or units[3] != 'JPY':
                state['valid'] = False;state['issue'] = 'この商品の原価・通貨の計算には未対応です。'
            if (account, ident) in missing_opening:
                state['valid'] = False;state['issue'] = '最初の取引より前の配当があり、開始原価を確認できません。'
            quantity = _decimal(row['quantity'], positive=True)
            amount = _decimal(row['settlement_amount'])
            unit = Decimal(row['quote_unit']);gain = Decimal(0)
            if row['side'] == '買':
                old_quantity = state['quantity'];old_average = state['average']
                state['quantity'] += quantity
                known = state['valid'] and old_quantity >= 0 and old_average is not None
                if known:
                    state['average'] = _rounded(((old_quantity * old_average / unit) + amount) / state['quantity'] * unit)
                else:
                    state['valid'] = False;state['average'] = None
                    state['issue'] = state['issue'] or '買付前の数量・取得原価を確認できません。'
                gain_known = known
            else:
                known = state['valid'] and state['quantity'] >= quantity and state['average'] is not None
                gain = amount - state['average'] / unit * quantity if known else None
                state['quantity'] -= quantity
                if not known:
                    state['valid'] = False;state['average'] = None
                    state['issue'] = state['issue'] or '売却前の買付履歴・開始原価が不足しています。'
                elif state['quantity'] == 0:state['average'] = Decimal(0)
                gain_known = known
            row.update(basis_known=state['valid'], gain_known=gain_known,
                       calculated_quantity=state['quantity'], calculated_average=state['average'],
                       calculated_gain=gain if gain_known else None, issue=state['issue'])
            output.append(row)
    # Python's stable sort preserves deterministic reverse accounting order.
    return list(reversed(output))


def _numeric(value):
    if value is None:return None
    result = _decimal(value)
    return int(result) if result == result.to_integral_value() else float(result)


def _account_labels(accounts):
    raw = {row['id']: short(row['label']) for row in accounts}
    normalized = {key: ''.join(unicodedata.normalize('NFKC', value).split()).casefold() for key, value in raw.items()}
    count = Counter(normalized.values())
    return {key: value + '（' + key + '）' if count[normalized[key]] > 1 else value for key, value in raw.items()}


def _previous_average(row, last=10000):
    if row >= last:return '0'
    start = row + 1
    return (f'IFERROR(INDEX(L{start}:$L${last},MATCH(1,INDEX('
            f'(B{start}:$B${last}=B{row})*(D{start}:$D${last}=D{row})*'
            f'(E{start}:$E${last}=E{row}),0),0)),0)')


def _literal_criteria(reference):
    """SUMIFS otherwise interprets valid account-name characters as patterns."""
    return (f'"="&SUBSTITUTE(SUBSTITUTE(SUBSTITUTE({reference},"~","~~"),'
            '"*","~*"),"?","~?")')


def _date(value):
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None or stamp.utcoffset() is None:raise ValueError()
        return stamp.astimezone(JST)
    except (ValueError, TypeError):return None


def _current_table(sheet, accounts, capture):
    capture = capture if isinstance(capture, dict) else {}
    stamp = _date(capture.get('captured_at'))
    sheet.put(1, 15, stamp.date().isoformat() if stamp else None, 'date')
    sheet.row(1, ['成長枠残高', '積立枠残高', '現金(円)', '現金(ドル)'], 16)
    labels = _account_labels(accounts)
    by_account = capture.get('accounts', {}) if isinstance(capture.get('accounts'), dict) else {}
    for r, profile in enumerate(sorted(accounts, key=lambda item: item['id']), 2):
        account = profile['id'];sheet.put(r, 15, labels[account])
        item = by_account.get(account, {}) if account != 'E' else {}
        values = item.get('values', {}) if isinstance(item, dict) else {}
        observed = _date(item.get('observed_at')) if isinstance(item, dict) else None
        if not stamp or not observed:values = {}
        for c, key in ((16, 'growth_remaining'), (17, 'tsumitate_remaining')):
            value = values.get(key)
            if isinstance(value, dict) and observed and value.get('year') == observed.year:
                value = value.get('remaining')
            else:value = None
            sheet.put(r, c, _numeric(value), 'number')
        sheet.put(r, 18, _numeric(values.get('cash_total')), 'number')
        sheet.put(r, 19, _numeric(values.get('usd_cash')), 'number')


def build_trade_sheet(store, activities, as_of, current_capture=None, today=None):
    """Return the requested A:M running calculation and O:S current snapshot."""
    boundary = parse_date(as_of)
    records = store.records()
    events = activities.events()
    rows = build_trade_rows(records, events, boundary)
    if len(rows) > MAX_ROWS:
        raise LedgerError('合算・分割後の取引行が9999行を超えます。計算式の範囲拡張が必要です。')
    accounts = store.accounts();labels = _account_labels(accounts)
    from security_names import load as load_names
    names = load_names(store.directory)
    sheet = Sheet('取引')
    sheet.row(1, HEADERS + ['保有株数', '平均取得単価', '確定損益'])
    sheet.widths = {str(c): width for c, width in enumerate(
        [13, 5.625, 15.625, 7.625, 13, 3.125, 7.75, 10.625, 16.625,
         8.625, 13, 9.5, 11.75, 9, 10.5, 10.625, 13, 13.625, 10.625], 1)}
    for r, row in enumerate(rows, 2):
        ident = reporting_code(row)
        fill = None if row['movement'] == 'split' else 'DDEBF7' if row['side'] == '買' else 'FCE4D6'
        values = [row['trade_date'], labels.get(row['account'], row.get('account_label', row['account'])),
                  names.get(ident, row.get('name', '')), code(ident), short(row['custody']), short(row['side']),
                  _numeric(row['quantity']), _numeric(row['price']), _numeric(row['settlement_amount']),
                  _numeric(_decimal(row['fees']) + _decimal(row['tax']))]
        for c, value in enumerate(values, 1):
            sheet.put(r, c, value, 'date' if c == 1 else 'code' if c == 4 else None, fill)
        account_criteria = _literal_criteria(f'B{r}')
        code_criteria = _literal_criteria(f'D{r}')
        custody_criteria = _literal_criteria(f'E{r}')
        quantity = (f'IF(A{r}="","",SUMIFS(G{r}:$G$10000,B{r}:$B$10000,{account_criteria},'
                    f'D{r}:$D$10000,{code_criteria},E{r}:$E$10000,{custody_criteria},F{r}:$F$10000,"B")-'
                    f'SUMIFS(G{r}:$G$10000,B{r}:$B$10000,{account_criteria},D{r}:$D$10000,{code_criteria},'
                    f'E{r}:$E$10000,{custody_criteria},F{r}:$F$10000,"S"))')
        sheet.formula(r, 11, quantity)
        if not row['basis_known']:
            average = f'IF(A{r}="","","")'
        else:
            previous = _previous_average(r)
            unit = row['quote_unit']
            average = (f'IF(A{r}="","",IF(K{r}=0,0,IF(F{r}="S",{previous},'
                       f'ROUNDUP((((K{r}-G{r})*{previous}/{unit})+I{r})/K{r}*{unit},0))))')
        sheet.formula(r, 12, average)
        gain = (f'IF(A{r}="","",IF(F{r}="S",I{r}-{_previous_average(r)}/{row["quote_unit"]}*G{r},0))'
                if row['gain_known'] or row['movement'] == 'split' else f'IF(A{r}="","","")')
        sheet.formula(r, 13, gain)
        for cell in sheet.cells[-3:]:
            cell['format'] = GAIN if cell['c'] == 13 else NUM
            cell['align'] = {'h': 'right', 'v': 'center', 'wrap': True}
    _current_table(sheet, accounts, current_capture)
    sheet.row_height = sheet.header_height = 20.1
    for cell in sheet.cells:
        cell.setdefault('align', {'h': 'center' if cell['c'] <= 6 or cell['c'] == 15 else 'right',
                                  'v': 'center', 'wrap': True})
        if cell['c'] in (7, 8, 9, 10, 11, 12, 13, 16, 17, 18, 19):
            cell.setdefault('format', NUM)
        if cell['r'] > 1 and cell['c'] == 8:cell['format'] = '#,##0.########'
        if cell['r'] > 1 and cell['c'] == 19:cell['format'] = '#,##0.00;[Red]-#,##0.00;0.00'
    result = sheet.result()
    result['filter'] = None
    result['issues'] = sorted(set(row['issue'] for row in rows if row['issue']))
    result['calculation_assumptions'] = ['同日の分割・買い・売りは、分割→買い→売りの日次順で計算します。',
        '同日・同口座・同銘柄・同預り・同売買を合算後、平均取得単価を円単位で切り上げます。',
        '原価は受渡金額による計算で、SBIの税務上の取得価額を照合したものではありません。']
    return result
