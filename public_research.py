"""Read-only public market pages; dated facts never become personal receipts.

Only a visible labelled historical row or a matching structured record is used.
An empty table is not evidence that an instrument pays no dividend.
"""
from datetime import date, datetime, timedelta, timezone
from copy import deepcopy
from html import unescape
from html.parser import HTMLParser
import json
import math
import re
import os
from pathlib import Path
import tempfile
import time
import unicodedata
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

JST = timezone(timedelta(hours=9))
MAX_PUBLIC_BYTES = 8 * 1024 * 1024
_CODE = re.compile(r'[0-9][0-9A-Z]{3}\Z')
_HOSTS = {'finance.yahoo.co.jp', 'jp.investing.com', 'www.investing.com', 'api.investing.com'}


class PublicReferenceError(ValueError):
    pass


def domestic_code(value):
    code = unicodedata.normalize('NFKC', str(value)).strip().upper()
    if not _CODE.fullmatch(code):
        raise PublicReferenceError('国内上場銘柄の4文字コードを確認してください。')
    return code


def public_text(url, opener=None, timeout=8):
    """Ordinary public GET, bounded response; no cookies or credentials."""
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in _HOSTS or parsed.username or parsed.password or parsed.port not in (None,443):
        raise PublicReferenceError('公開参考情報の取得先を確認できません。')
    request = Request(url, headers={'User-Agent': 'Mozilla/5.0',
                                   'Accept-Language': 'ja,en;q=0.7',
                                   'Accept': 'text/html,application/json;q=0.9'})
    try:
        with (opener or urlopen)(request, timeout=timeout) as response:
            final = urlsplit(response.geturl())
            if final.scheme != 'https' or final.hostname not in _HOSTS or final.username or final.password or final.port not in (None,443):
                raise PublicReferenceError('公開参考情報の転送先を確認できません。')
            raw = response.read(MAX_PUBLIC_BYTES + 1)
            if len(raw) > MAX_PUBLIC_BYTES:
                raise PublicReferenceError('公開参考情報の応答が上限を超えています。')
            return raw.decode('utf-8-sig'), response.geturl()
    except PublicReferenceError:
        raise
    except Exception:
        raise PublicReferenceError('公開サイトへ接続できません。アクセス制限や通信状態を確認してください。') from None


def _compact(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value))).casefold()


def _amount(value):
    if isinstance(value, bool):
        return None
    text = unicodedata.normalize('NFKC', str(value)).strip().replace(',', '')
    text = re.sub(r'^(?:JPY|¥|￥)\s*|\s*(?:円|JPY)$', '', text, flags=re.I)
    try:
        number = float(text)
        return number if math.isfinite(number) and number >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _day(value):
    if isinstance(value, datetime):
        return (value.astimezone(JST) if value.tzinfo else value).date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize('NFKC', value).strip()
    # Public tables may append a Japanese weekday or emit an ISO midnight
    # timestamp. Neither changes the exchange calendar date being requested.
    text=re.sub(r'\s*\([月火水木金土日](?:曜(?:日)?)?\)$','',text)
    if 'T' in text or re.match(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}',text):
        try:
            stamp = datetime.fromisoformat(text.replace('Z','+00:00'))
            return (stamp.astimezone(JST) if stamp.tzinfo else stamp).date()
        except ValueError:pass
    match = re.fullmatch(r'(\d{4})[年/.-](\d{1,2})[月/.-](\d{1,2})日?', text)
    if match:
        try:
            return date(*map(int, match.groups()))
        except ValueError:
            return None
    for pattern in ('%b %d, %Y', '%B %d, %Y', '%d %b %Y', '%d.%m.%Y', '%d/%m/%Y'):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            pass
    return None


class _Page(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.tables = []; self.identities = []; self.links = []; self.json = [];self.paragraphs = []
        self.text = []; self._table = None; self._depth = 0
        self._row = None; self._cell = None; self._identity = None; self._script = None;self._in_script = False;self._paragraph = None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'p':self._paragraph = []
        if tag in ('h1', 'title'):
            self._identity = []
        if tag == 'meta' and values.get('property') == 'og:title':
            self.identities.append(values.get('content', ''))
        if tag == 'a' and values.get('href'):
            self.links.append(values['href'])
        if tag == 'script':
            self._in_script = True
            if (values.get('type') in ('application/json', 'application/ld+json')
                    or values.get('id') == '__NEXT_DATA__'):
                self._script = []
        if tag == 'table':
            if self._depth == 0:
                self._table = []
            self._depth += 1
        if self._depth and tag == 'tr':
            self._row = []
        if self._depth and tag in ('td', 'th'):
            self._cell = []

    def handle_data(self, value):
        if self._in_script:
            if self._script is not None:self._script.append(value)
            return
        self.text.append(value)
        if self._paragraph is not None:self._paragraph.append(value)
        if self._identity is not None:
            self._identity.append(value)
        if self._cell is not None:
            self._cell.append(value)

    def handle_endtag(self, tag):
        if tag == 'p' and self._paragraph is not None:
            self.paragraphs.append(''.join(self._paragraph));self._paragraph = None
        if tag == 'script':self._in_script = False
        if tag == 'script' and self._script is not None:
            try:
                self.json.append(json.loads(''.join(self._script)))
            except (ValueError, TypeError, RecursionError):
                pass
            self._script = None
        if tag in ('h1', 'title') and self._identity is not None:
            self.identities.append(''.join(self._identity)); self._identity = None
        if tag in ('td', 'th') and self._cell is not None:
            if self._row is not None:
                self._row.append(' '.join(''.join(self._cell).split()))
            self._cell = None
        if tag == 'tr' and self._row is not None:
            if self._table is not None:
                self._table.append(self._row)
            self._row = None
        if tag == 'table' and self._depth:
            self._depth -= 1
            if self._depth == 0 and self._table is not None:
                self.tables.append(self._table); self._table = None


def _objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)


def _matches_symbol(value, code):
    return str(value).strip().upper() in (code, code + '.T', 'TYO:' + code, 'TSE:' + code)


def _identity(page, code):
    bracketed = {match for text in page.identities for match in re.findall(
        r'[（(【]\s*([0-9][0-9A-Z]{3})(?:\.T)?\s*[）)】]',
        unicodedata.normalize('NFKC',text).upper())}
    if bracketed and code not in bracketed:return False
    for text in page.identities:
        if re.search(r'(?<![A-Z0-9])' + re.escape(code) + r'(?:\.T)?(?![A-Z0-9])',
                     unicodedata.normalize('NFKC', text).upper()):
            return True
    # A related-instrument widget cannot establish the owner of a whole page.
    symbols = {str(obj[key]).upper().removesuffix('.T')
               for blob in page.json for obj in _objects(blob)
               for key in ('symbol', 'ticker', 'stockSymbol', 'displaySymbol', 'symbolCode')
               if key in obj and _CODE.fullmatch(str(obj[key]).upper().removesuffix('.T'))}
    return symbols == {code}


def _owned_objects(blobs, code, container_names):
    """Read JSON rows only beneath a symbol-bound historical container.

    A page may include recommendations and widgets for unrelated instruments.
    Its headline alone therefore cannot bind every recursive date/price pair.
    """
    symbol_keys = ('symbol', 'ticker', 'stockSymbol', 'displaySymbol', 'symbolCode')
    def visit(node, bound=False, rows=False):
        if isinstance(node, dict):
            identities = [node[key] for key in symbol_keys if node.get(key) is not None]
            for key in ('instrument', 'security', 'instrumentInfo', 'symbolInfo'):
                if isinstance(node.get(key), dict):
                    identities.extend(node[key][field] for field in symbol_keys if node[key].get(field) is not None)
            if identities:
                if not all(_matches_symbol(value, code) for value in identities):return
                bound = True
            if bound and rows:yield node
            for key, value in node.items():
                if _compact(key) in ('recommendations','relatedinstruments','relatedsymbols','peers'):continue
                yield from visit(value, bound, rows or bound and _compact(key) in container_names)
        elif isinstance(node, list):
            for value in node:yield from visit(value, bound, rows)
    for blob in blobs:yield from visit(blob)


def _columns(row, groups):
    normal = [_compact(item) for item in row]
    found = {}
    for key, choices in groups.items():
        for index, label in enumerate(normal):
            if label in choices:
                found[key] = index
                break
    return found


def parse_yahoo_history(text, code):
    """Parse the unadjusted 終値 column, never 調整後終値."""
    code = domestic_code(code); page = _Page(text)
    if not _identity(page, code):
        raise PublicReferenceError('Yahoo時系列の銘柄を確認できません。')
    bars = []
    groups = {'date': {'日付', 'date'}, 'close': {'終値', '終値(円)', 'close'}}
    for table in page.tables:
        columns = None
        for row in table:
            found = _columns(row, groups)
            if set(found) == {'date', 'close'}:
                columns = found
                continue
            if columns and len(row) > max(columns.values()):
                day = _day(row[columns['date']]); amount = _amount(row[columns['close']])
                if day and amount is not None and amount > 0:
                    bars.append((day, amount))
    if not bars:
        for obj in _owned_objects(page.json, code, {'historicaldata','historicalprices','histories','history','pricehistory'}):
            # Explicit date/close pairs, not current quotes or adjusted closes.
            day = _day(obj.get('date', obj.get('tradingDate')))
            amount = _amount(obj.get('close', obj.get('closingPrice')))
            if day and amount is not None and amount > 0:
                bars.append((day, amount))
    grouped = {}
    for day, amount in bars:
        grouped.setdefault(day, set()).add(amount)
    return sorted((day, next(iter(values))) for day, values in grouped.items() if len(values) == 1)


def yahoo_japan_month_closes(code, month, opener=None, now=None):
    code = domestic_code(code); first = date.fromisoformat(month + '-01')
    from jpx_calendar import first_trading_day,CALENDAR_SOURCE
    opening = first_trading_day(month)
    moment = now or datetime.now(JST)
    if moment.tzinfo is None or first > moment.astimezone(JST).date():
        raise PublicReferenceError('対象月の終値はまだ確認できません。')
    moment = moment.astimezone(JST); previous = first - timedelta(days=1)
    url = f'https://finance.yahoo.co.jp/quote/{code}.T/history?' + urlencode({
        'from': previous.replace(day=1).strftime('%Y%m%d'),
        'to': (opening + timedelta(days=1)).strftime('%Y%m%d'), 'timeFrame': 'd'})
    text, source = public_text(url, opener=opener)
    bars = parse_yahoo_history(text, code)
    older = [(day, amount) for day, amount in bars if 0 <= (previous-day).days <= 7]
    result = {}
    if older:
        day, amount = max(older)
        result.update(close=amount, close_date=day.isoformat(), price_source=source)
    ready = first < moment.date() or (moment.hour, moment.minute) >= (16, 0)
    exact = [(day, amount) for day, amount in bars if day == first]
    if ready and len(exact) == 1:
        day, amount = exact[0]
        result.update(month_start_close=amount, month_start_close_date=day.isoformat(),
                      month_start_price_source=source)
    ready = opening < moment.date() or opening == moment.date() and (moment.hour,moment.minute) >= (16,0)
    exact = [(day,amount) for day,amount in bars if day == opening]
    if ready and len(exact) == 1:
        day,amount = exact[0]
        result.update(month_open_close=amount,month_open_close_date=day.isoformat(),
                      month_open_price_source=source,month_open_calendar_source=CALENDAR_SOURCE)
    if not result:
        raise PublicReferenceError('Yahoo時系列に対象日の終値を確認できません。')
    return result


def investing_url(value):
    if not isinstance(value, str):
        return None
    parsed = urlsplit(unescape(value))
    if (parsed.scheme != 'https' or parsed.hostname not in ('jp.investing.com', 'www.investing.com')
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        return None
    if not re.fullmatch(r'/equities/[A-Za-z0-9%.,_-]+(?:-dividends|-historical-data)?', parsed.path):
        return None
    stem = re.sub(r'-(?:dividends|historical-data)$', '', parsed.path)
    return urlunsplit(('https', 'jp.investing.com', stem + '-dividends', '', ''))


def parse_investing_dividends(text, code, year, source):
    code = domestic_code(code)
    if not investing_url(source):
        raise PublicReferenceError('配当情報の出典を確認できません。')
    page = _Page(text)
    if not _identity(page, code):
        raise PublicReferenceError('Investing.com配当情報の銘柄を確認できません。')
    events = []
    groups = {'ex_date': {'権利落ち日', '配当落ち日', 'ex-dividenddate', 'exdividenddate'},
              'payment': {'支払日', '支払い日', '配当金支払日', 'paymentdate'},
              'dps': {'配当', '配当金', 'dividend', 'dividends', 'dividendpershare'}}
    for table in page.tables:
        columns = None
        for row in table:
            found = _columns(row, groups)
            if 'ex_date' in found and 'payment' in found:
                columns = found
                continue
            if not columns or len(row) <= max(columns.values()):
                continue
            ex = _day(row[columns['ex_date']]); payment = _day(row[columns['payment']])
            if not (ex and ex.year == year or payment and payment.year == year):
                continue
            event = {'ex_date': ex.isoformat() if ex else None,
                     'payment': payment.isoformat() if payment else None,
                     'payment_year': payment.year if payment else None,
                     'source': source}
            if 'dps' in columns:
                event['dps'] = _amount(row[columns['dps']])
            events.append(event)
    if not events:
        # Modern pages may expose the same explicitly named rows in Next data.
        for obj in _owned_objects(page.json, code, {'dividends','dividendhistory','dividenddata','dividendrows'}):
            ex = _day(obj.get('exDate', obj.get('ex_dividend_date', obj.get('exDividendDate'))))
            payment = _day(obj.get('paymentDate', obj.get('payment_date')))
            if not (ex and ex.year == year or payment and payment.year == year):
                continue
            events.append({'ex_date': ex.isoformat() if ex else None,
                           'payment': payment.isoformat() if payment else None,
                           'payment_year': payment.year if payment else None,
                           'dps': _amount(obj.get('dividend', obj.get('dividendAmount'))),
                           'source': source})
    by_key = {}
    for event in events:
        key = (event.get('ex_date'), event.get('payment'))
        if key in by_key and by_key[key].get('dps') != event.get('dps'):
            raise PublicReferenceError('同じ配当予定の金額が競合しています。')
        if any(old.get('ex_date') == event.get('ex_date') and old.get('payment') != event.get('payment')
               for old in by_key.values() if old.get('ex_date') is not None):
            raise PublicReferenceError('同じ権利落ち日の配当支払日が競合しています。')
        by_key[key] = event
    # Absence, a dash, or a zero yield does not establish a zero-dividend policy.
    names = []
    for identity in page.identities:
        match = re.match(r'(.+?)\s*[（(【]\s*'+re.escape(code)+r'(?:\.T)?\s*[）)】]', identity)
        if match and len(match.group(1).strip()) >= 2:names.append(match.group(1).strip().casefold())
    subject = r'(?<![\w])(?:'+'|'.join(re.escape(name) for name in [code]+names)+r')(?:\s*[（(【]\s*'+re.escape(code)+r'(?:\.T)?\s*[）)】])?'
    policy = re.compile(r'(?:(?:No,|いいえ[、,]|現在[、,])\s*)?'+subject+r'\s*(?:(?:does\s+not\s+(?:currently\s+)?pay\s+(?:(?:a|any)\s+)?dividends)|(?:は|が)?\s*(?:配当(?:を|は)?支払って(?:いません|おりません)|無配(?:方針|当|です|である)))(?:です|となっています)?[。.!！]?',re.I)
    positive_policy = re.compile(subject+r'\s*(?:(?:currently\s+)?pays?\s+(?:a\s+)?dividends|(?:は|が)?\s*配当(?:を|は)?支払って(?:います|おります))',re.I)
    no_dividend = False
    for paragraph in page.paragraphs:
        other = set(re.findall(r'[（(【]\s*([0-9][0-9A-Z]{3})\s*[）)】]',paragraph.upper()))-{code}
        if not other and policy.fullmatch(paragraph.strip()) and not positive_policy.search(paragraph):
            no_dividend = True
    if any(event.get('dps', 0) and event.get('dps', 0) > 0 for event in by_key.values()):
        no_dividend = False
    result = {'code': code, 'year': year, 'source': source,
              'fetched_at': datetime.now(JST).isoformat(),
              'events': sorted(by_key.values(), key=lambda e: (e.get('ex_date') or '', e.get('payment') or ''))}
    if no_dividend:
        result.update(no_dividend=True, no_dividend_source=source)
    return result


def _search_url(text, code):
    """Accept only an exact domestic ticker with an explicit Japan exchange."""
    try:
        objects = list(_objects(json.loads(text)))
    except (ValueError, TypeError):
        page = _Page(text); objects = [item for blob in page.json for item in _objects(blob)]
    matches = set()
    for obj in objects:
        symbol = next((obj.get(key) for key in ('symbol', 'ticker', 'stockSymbol') if obj.get(key) is not None), None)
        exchange = str(obj.get('exchange', obj.get('exchangeName', obj.get('exchange_name', '')))).casefold()
        if not _matches_symbol(symbol, code) or not any(token in exchange for token in ('tokyo', 'japan', '東京', 'tyo', 'tse', '東証')):
            continue
        candidate = obj.get('link', obj.get('url', obj.get('canonicalUrl')))
        if isinstance(candidate, str) and candidate.startswith('/equities/'):
            candidate = 'https://jp.investing.com' + candidate
        candidate = investing_url(candidate)
        if candidate:
            matches.add(candidate)
    return next(iter(matches)) if len(matches) == 1 else None


def fetch_investing_dividends(code, year, existing=None, opener=None):
    code = domestic_code(code); existing = existing or {}
    candidates = []
    for key in ('investing_url', 'dividend_source'):
        value = investing_url(existing.get(key))
        if value:
            candidates.append(value)
    for raw in re.findall(r'https://(?:jp|www)\.investing\.com/[^\s;；]+', str(existing.get('source', ''))):
        value = investing_url(raw)
        if value:
            candidates.append(value)
    if code == '6146':
        # Exact user-supplied seed; the fetched page must still verify ticker.
        candidates.append('https://jp.investing.com/equities/disco-corp-dividends')
    if not candidates:
        search = 'https://api.investing.com/api/search/v2/search?' + urlencode({'q': code, 'tab': 'quotes', 'limit': 20})
        text, _ = public_text(search, opener=opener)
        candidate = _search_url(text, code)
        if candidate:
            candidates.append(candidate)
    for url in list(dict.fromkeys(candidates))[:3]:
        try:
            text, final = public_text(url, opener=opener)
            # Redirects must remain on the exact verified instrument slug.
            if investing_url(final) != url:continue
            result = parse_investing_dividends(text, code, year, final)
            if result['events'] or result.get('no_dividend'):return result
        except PublicReferenceError:continue
    raise PublicReferenceError('対象銘柄の今年の配当支払日を確認できません。')


def merge_investing_reference(existing, reference, year, code):
    """Fill missing public dates, preserving verified annual/manual facts."""
    code = domestic_code(code)
    if (not isinstance(reference, dict) or reference.get('code') != code
            or type(reference.get('year')) is not int or reference['year'] != year
            or not investing_url(reference.get('source'))
            or not isinstance(reference.get('events'), list)):
        raise PublicReferenceError('配当参考情報の銘柄・対象年・出典を確認できません。')
    rows = []
    for original in reference['events']:
        if not isinstance(original, dict):
            raise PublicReferenceError('配当参考情報の形式を確認できません。')
        row = deepcopy(original)
        for key in ('ex_date', 'payment'):
            if row.get(key) is not None and (_day(row[key]) is None or _day(row[key]).isoformat() != row[key]):
                raise PublicReferenceError('配当参考情報の日付を確認できません。')
        if row.get('dps') is not None and _amount(row['dps']) is None:
            raise PublicReferenceError('配当参考情報の金額を確認できません。')
        ex = _day(row.get('ex_date')); pay = _day(row.get('payment'))
        if not (ex and ex.year == year or pay and pay.year == year):
            continue
        if pay:
            row['payment_year'] = pay.year
        row['source'] = reference['source']
        rows.append(row)
    result = deepcopy(existing)
    ref = deepcopy(reference);ref['events'] = rows
    result['investing_reference'] = ref
    result['investing_url'] = investing_url(reference['source'])
    result['dividend_source'] = reference['source']
    result.setdefault('source', reference['source'])
    events = result.setdefault('events', [])
    if not isinstance(events, list):
        raise PublicReferenceError('既存の配当予定を確認できません。')
    accepted_payments = set()
    for incoming in rows:
        ex = _day(incoming.get('ex_date')); pay = incoming.get('payment')
        if not pay:
            continue
        exact = [item for item in events if isinstance(item, dict) and
                 (ex and item.get('ex_date') == ex.isoformat() or item.get('payment') == pay)]
        if not exact and ex:
            # Match an already verified entitlement, never derive it from
            # a weekend-only approximation of Japan's exchange calendar.
            candidates = []
            for item in events:
                rights = _day(item.get('rights')) if isinstance(item, dict) else None
                if (rights and 1 <= (ex-rights).days <= 7
                        and _amount(item.get('dps')) is not None
                        and _amount(item.get('dps')) == _amount(incoming.get('dps'))):
                    candidates.append(item)
            if len(candidates) == 1:
                exact = candidates
        if len(exact) == 1:
            item = exact[0]
            if not item.get('payment') and item.get('payment_year') in (None, int(pay[:4])):
                item['payment'] = pay;item['payment_year'] = int(pay[:4])
                item.setdefault('ex_date', incoming.get('ex_date'))
                item['payment_source'] = reference['source']
                accepted_payments.add(pay)
            elif item.get('payment') == pay:
                accepted_payments.add(pay)
            # Existing dates and DPS remain authoritative, even on disagreement.
        elif not exact:
            events.append(deepcopy(incoming))
            accepted_payments.add(pay)
    from date_highlights import dates_in
    known = {day.isoformat() for day in dates_in(result.get('payments', ''))}
    payments = {pay for pay in accepted_payments if pay.startswith(str(year)+'-')}
    additions = sorted(payments-known)
    if additions:
        before = result.get('payments') or ''
        result['payments'] = '・'.join(([before] if before else []) + additions)
    if reference.get('no_dividend') is not None and type(reference['no_dividend']) is not bool:
        raise PublicReferenceError('無配の確認情報を確認できません。')
    if reference.get('no_dividend'):
        if (not isinstance(reference.get('no_dividend_source'), str)
                or not reference['no_dividend_source'].strip()):
            raise PublicReferenceError('無配の出典を確認できません。')
        positive = _amount(result.get('dps'))
        if not positive and not any((_amount(item.get('dps')) or 0) > 0 for item in events):
            result['no_dividend'] = True
            result['no_dividend_source'] = reference['no_dividend_source']
    return result


def _explicit_no_dividend(fact):
    if fact.get('no_dividend') is True and isinstance(fact.get('no_dividend_source'), str) and fact['no_dividend_source'].strip():
        return fact['no_dividend_source']
    # Existing verified official zero-distribution policies have been retained
    # since the original app. Zero DPS by itself is insufficient evidence.
    if (_amount(fact.get('dps')) == 0 and fact.get('calendar_complete') is True
            and isinstance(fact.get('source'), str) and fact['source'].strip()
            and re.search(r'無配|無分配', ' '.join(str(fact.get(key, '')) for key in ('payments','rights','status')))):
        return fact['source']
    return None


def _write_research(directory, year, data):
    folder = Path(directory)/'market';folder.mkdir(parents=True, exist_ok=True)
    target = folder/f'research_{year}.json'
    if folder.is_symlink() or target.is_symlink():
        raise PublicReferenceError('公開参考情報の通常の保存先を確認してください。')
    descriptor, temporary = tempfile.mkstemp(prefix='.research_', suffix='.tmp', dir=folder)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush();os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def _needs_prices(fact, first, moment):
    previous = first-timedelta(days=1)
    from jpx_calendar import first_trading_day
    opening = first_trading_day(first.strftime('%Y-%m'))
    prior = _day(fact.get('close_date'))
    ready = opening < moment.date() or opening == moment.date() and (moment.hour,moment.minute) >= (16,0)
    prior_known = (prior is not None and 0 <= (previous-prior).days <= 7
                   and prior < first and (_amount(fact.get('close')) or 0) > 0)
    start_known=False
    for prefix in ['month_open']+(['month_start'] if opening==first else []):
        start=_day(fact.get(prefix+'_close_date'));amount=fact.get(prefix+'_close');source=fact.get(prefix+'_price_source')
        if start==opening and (_amount(amount) or 0)>0 and isinstance(source,str) and source.strip():
            start_known=True;break
    return not prior_known or ready and not start_known


def refresh_public_research(directory, codes, month, *, cancelled=lambda:False,
                            budget_seconds=30, now=None, price_provider=None,
                            dividend_provider=None):
    """Bounded concurrent research; only the caller thread may publish files.

    The caller should pass an isolated staging directory, just as for PDF
    registration. Public rows are never added to the personal income ledger.
    """
    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
    from market_data import load_research, refresh_month_closes
    from yahoo_market import month_closes
    if (isinstance(budget_seconds, bool) or not isinstance(budget_seconds, (int,float))
            or not math.isfinite(budget_seconds) or budget_seconds <= 0):
        raise PublicReferenceError('公開情報取得の待ち時間を確認してください。')
    first = date.fromisoformat(month+'-01')
    if first.strftime('%Y-%m') != month:
        raise PublicReferenceError('対象月をYYYY-MMで指定してください。')
    moment = now or datetime.now(JST)
    if moment.tzinfo is None:
        raise PublicReferenceError('現在時刻にタイムゾーンを指定してください。')
    moment = moment.astimezone(JST);year = moment.year
    codes = sorted({str(value).upper() for value in codes if _CODE.fullmatch(str(value).upper())})
    result = dict(updated=0, failed=[], written=False, price_failed=[], dividend_failed=[])
    if cancelled():
        return dict(result, cancelled=True, failed=codes)
    data = load_research(directory, year)
    old_prices = load_research(directory, first.year)
    # A corrupt user cache is preserved; other years may still be independent.
    div_facts = data.get('securities', {})
    price_provider = price_provider or month_closes
    dividend_provider = dividend_provider or fetch_investing_dividends
    prices = {}; dividends = {}; errors = {'price':set(), 'dividend':set()}
    zero_policies = {code:_explicit_no_dividend(div_facts.get(code, {})) for code in codes}
    pool = ThreadPoolExecutor(max_workers=6)
    pending = {};deadline = time.monotonic()+budget_seconds
    try:
        for code in codes:
            if not old_prices.get('_load_error') and first <= moment.date():
                if _needs_prices(old_prices['securities'].get(code, {}), first, moment):
                    pending[pool.submit(price_provider, code, month)] = ('price', code)
            else:errors['price'].add(code)
            if data.get('_load_error'):
                errors['dividend'].add(code)
            elif not zero_policies[code]:
                pending[pool.submit(dividend_provider, code, year, deepcopy(div_facts.get(code, {})))] = ('dividend', code)
        while pending:
            if cancelled():
                return dict(result, cancelled=True, failed=codes)
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                for kind, code in pending.values():errors[kind].add(code)
                break
            done, _ = wait(pending, timeout=min(.2, remaining), return_when=FIRST_COMPLETED)
            for future in done:
                kind, code = pending.pop(future)
                try:
                    value = future.result()
                    if not isinstance(value, dict):raise ValueError()
                    (prices if kind == 'price' else dividends)[code] = value
                except Exception:
                    errors[kind].add(code)
    finally:
        for future in pending:future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
    if cancelled():return dict(result, cancelled=True, failed=codes)
    if not old_prices.get('_load_error'):
        merged = refresh_month_closes(directory, codes, month, provider=lambda code, selected:prices.get(code, {}),
                                      cancelled=cancelled, budget_seconds=2, now=moment,include_month_open=True)
        result['updated'] += merged['updated'];result['written'] |= merged['written']
        errors['price'].update(merged['failed'])
    # Reload after prices: selected month may share the same year's file.
    data = load_research(directory, year)
    changed = 0
    if not data.get('_load_error'):
        for code in codes:
            if cancelled():return dict(result, cancelled=True, failed=codes)
            old = data['securities'].get(code, {})
            try:
                if zero_policies[code]:
                    new = deepcopy(old)
                    new.update(no_dividend=True, no_dividend_source=zero_policies[code])
                elif code in dividends:
                    new = merge_investing_reference(old, dividends[code], year, code)
                else:continue
                if new != old:
                    data['securities'][code] = new;changed += 1
            except (ValueError, TypeError):
                errors['dividend'].add(code)
        if changed and not cancelled():
            _write_research(directory, year, data)
            result['updated'] += changed;result['written'] = True
    result['price_failed'] = sorted(errors['price'])
    result['dividend_failed'] = sorted(errors['dividend'])
    result['failed'] = sorted(errors['price'] | errors['dividend'])
    return result
