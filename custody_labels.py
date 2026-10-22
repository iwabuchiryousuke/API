"""Display normalization only. Original payloads and duplicate signatures stay intact."""
import re
import unicodedata

def label(value, day='', side='', asset_type='国内株式'):
    s=re.sub(r'\s+','',unicodedata.normalize('NFKC',str(value)))
    if 'NISA' not in s:return s
    if '旧' in s:return '旧NISA'
    if '成' in s:return 'NISA(成)'
    if '積' in s or 'つみたて' in s:return 'NISA(積)'
    if day and day<'2024-01-01':return '旧NISA'
    if side=='買' and asset_type=='国内株式':return 'NISA(成)'
    return 'NISA(要確認)'

def trade_label(t, trades):
    result=label(t.get('custody',''),t.get('trade_date',''),t.get('side',''),t.get('asset_type','国内株式'))
    if result=='NISA(要確認)':
        options={label(x.get('custody',''),x['trade_date'],x['side'],x.get('asset_type','国内株式')) for x in trades
                 if x['account']==t['account'] and x['code']==t['code'] and (bool(t['code']) or x.get('name')==t.get('name')) and x['trade_date']<=t['trade_date'] and x['side']=='買' and 'NISA' in x.get('custody','')}
        if len(options)==1 and 'NISA(要確認)' not in options:return options.pop()
    return result

def event_label(e,trades):
    return trade_label(dict(e,trade_date=e['date'],side='',custody=e.get('custody','')),trades)
