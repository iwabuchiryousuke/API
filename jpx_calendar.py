"""Japanese cash-equity trading days under the 2025-onward holiday rules.

The national-holiday rules follow the Cabinet Office holiday law. JPX adds
Saturday/Sunday, 2–3 January and 31 December closures. Equinox dates use the
National Astronomical Observatory's 1980–2099 approximation; changes in holiday
law or an exceptional exchange closure require updating this calendar.
"""
from datetime import date, timedelta
from functools import lru_cache
import re


MIN_YEAR = 2025
MAX_YEAR = 2099
CALENDAR_SOURCE = ('JPX cash-equity holidays / https://www.jpx.co.jp/corporate/about-jpx/calendar/ ; '
                   'Japan holiday law / https://www8.cao.go.jp/chosei/shukujitsu/gaiyou.html ; '
                   '2025-onward statutory rules, astronomical equinox approximation through 2099')


def _year(year):
    if type(year) is not int or not MIN_YEAR <= year <= MAX_YEAR:
        raise ValueError('取引日カレンダーは2025～2099年に対応しています。')
    return year


def _monday(year, month, ordinal):
    first = date(year, month, 1)
    return first + timedelta(days=(7-first.weekday()) % 7 + 7*(ordinal-1))


@lru_cache(maxsize=75)
def national_holidays(year):
    """Return statutory holidays, including citizens' and substitute holidays."""
    year = _year(year)
    vernal = int(20.8431 + .242194*(year-1980) - (year-1980)//4)
    autumn = int(23.2488 + .242194*(year-1980) - (year-1980)//4)
    base = {date(year, month, day) for month, day in (
        (1,1),(2,11),(2,23),(3,vernal),(4,29),(5,3),(5,4),(5,5),
        (8,11),(9,autumn),(11,3),(11,23))}
    base.update((_monday(year,1,2),_monday(year,7,3),_monday(year,9,3),_monday(year,10,2)))
    holidays = set(base)
    # Article 3(3): a weekday sandwiched between two national holidays.
    for day in base:
        middle = day + timedelta(days=1)
        if middle.weekday() != 6 and middle+timedelta(days=1) in base:
            holidays.add(middle)
    # Article 3(2): a Sunday national holiday moves to the next nonholiday.
    for day in sorted(base):
        if day.weekday() != 6:
            continue
        observed = day+timedelta(days=1)
        while observed in holidays:
            observed += timedelta(days=1)
        holidays.add(observed)
    return frozenset(holidays)


def is_trading_day(day):
    if type(day) is not date:
        raise ValueError('取引日を日付で指定してください。')
    _year(day.year)
    return (day.weekday() < 5 and day not in national_holidays(day.year)
            and not (day.month == 1 and day.day <= 3)
            and not (day.month == 12 and day.day == 31))


def first_trading_day(month):
    """Return the expected first JPX cash-equity trading date, never a quote minimum."""
    if not isinstance(month, str) or not re.fullmatch(r'\d{4}-\d{2}', month):
        raise ValueError('対象月をYYYY-MMで指定してください。')
    day = date.fromisoformat(month+'-01')
    _year(day.year)
    while not is_trading_day(day):
        day += timedelta(days=1)
    return day


def opening_day(year, month):
    _year(year)
    if type(month) is not int or not 1 <= month <= 12:
        raise ValueError('対象月を1～12の整数で指定してください。')
    return first_trading_day(f'{year:04}-{month:02}')
