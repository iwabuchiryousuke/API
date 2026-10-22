"""Personal-use Yahoo reference data. Never convert ex-dates into paid income."""
import math,re
from datetime import date,datetime,timedelta,timezone

def number(value):
    try:v=float(value)
    except (TypeError,ValueError):return None
    return v if math.isfinite(v) and v>=0 else None

def iso(value):
    if isinstance(value,datetime):return value.date().isoformat()
    if isinstance(value,date):return value.isoformat()
    return None

def fetch(code,year,factory=None):
    if not re.fullmatch(r'[0-9][0-9A-Z]{3}',code):raise ValueError('国内上場銘柄コードを指定してください。')
    if factory is None:
        import yfinance as yf
        factory=yf.Ticker
    ticker=factory(code+'.T')
    history=ticker.history(start=f'{year-1}-01-01',end=f'{year+1}-01-01',auto_adjust=False,actions=True,raise_errors=True,timeout=20)
    if history.empty:raise ValueError('株価・配当履歴が取得できません。既存値は維持します。')
    calendar=ticker.calendar or {}
    # Only matched historical ex-dividend entries; these are not payment dates.
    dividends=[]
    if 'Dividends' in history:
        for stamp,value in history['Dividends'].items():
            amount=number(value)
            if amount and stamp.year==year:dividends.append({'ex_date':iso(stamp),'dps':amount})
    ex=iso(calendar.get('Ex-Dividend Date'));pay=iso(calendar.get('Dividend Date'))
    return {'source':f'https://finance.yahoo.com/quote/{code}.T/ (yfinance)',
            'fetched_at':datetime.now(timezone.utc).isoformat(),
            'reference_only':True,'historical_dividends':dividends,
            'ex_date':ex,'payment_date':pay,
            'status':'参考情報。権利落ち日と入金日は別。年間DPS・年間予定の完全性・本決算日は未確認。'}

def merge_reference(existing,reference,year):
    result=dict(existing);result['yahoo_reference']=reference
    # Do not erase verified dates, annual DPS, or per-event plans with incomplete Yahoo data.
    if not result.get('payments') and reference.get('payment_date'):
        p=reference['payment_date'];ex=reference.get('ex_date')
        if p.startswith(str(year)) or (ex and ex.startswith(str(year)) and p.startswith(str(year+1))):
            result['payments']=p
    result['status']=reference['status']
    if not result.get('source'):result['source']=reference['source']
    return result

def previous_close(code,month,factory=None):
    if not re.fullmatch(r'[0-9][0-9A-Z]{3}',code):raise ValueError('国内コードを指定してください。')
    end=date.fromisoformat(month+'-01');start=(end-timedelta(days=1)).replace(day=1)
    if end>date.today().replace(day=1):raise ValueError('将来月は取得できません。')
    if factory is None:
        import yfinance as yf
        factory=yf.Ticker
    h=factory(code+'.T').history(start=start.isoformat(),end=end.isoformat(),auto_adjust=False,actions=False,raise_errors=True,timeout=20)
    if h.empty:raise ValueError('前月の終値を確認できません。')
    series=h['Close'].dropna()
    if series.empty:raise ValueError('前月の終値が空です。')
    value=number(series.iloc[-1]);day=series.index[-1].date()
    if value is None or not start<=day<end or (end-day).days>7:raise ValueError('月末近辺の終値を確認できません。')
    return {'close':value,'close_date':day.isoformat(),'price_source':f'Yahoo Finance via yfinance / {code}.T / unadjusted close'}
