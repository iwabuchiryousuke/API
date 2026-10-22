"""Dated public research + optional JPX official closing-price API.
Private API credentials are stored only via Windows DPAPI, never here.
"""
import json,calendar
from pathlib import Path
from datetime import date,timedelta
from urllib.request import Request,urlopen
from urllib.parse import urlencode

def load_research(directory,year):
    path=Path(directory)/'market'/f'research_{year}.json'
    if not path.exists():path=Path(__file__).parent/'reference'/f'research_{year}.json'
    if not path.exists():return {'securities':{}}
    value=json.loads(path.read_text(encoding='utf-8'))
    if value.get('year')!=year:return {'securities':{}}
    return value

def previous_close(code,month,api_key):
    y,m=map(int,month.split('-'));end=date(y,m,1)-timedelta(days=1);start=end.replace(day=1)
    params={'code':str(code),'from':start.isoformat(),'to':end.isoformat()};rows=[]
    for _ in range(100):
        req=Request('https://api.jquants.com/v2/equities/bars/daily?'+urlencode(params),headers={'x-api-key':api_key})
        with urlopen(req,timeout=30) as response:obj=json.load(response)
        rows+=obj.get('data',[])
        if not obj.get('pagination_key'):break
        params['pagination_key']=obj['pagination_key']
    else:raise ValueError('株価APIのページ数上限に達しました。')
    rows=[r for r in rows if r.get('C') is not None and start.isoformat()<=r['Date']<=end.isoformat() and str(r['Code']) in (str(code),str(code)+'0')]
    if not rows:return None
    r=max(rows,key=lambda r:r['Date']);return {'close':r['C'],'close_date':r['Date'],'price_source':'JPX J-Quants /equities/bars/daily (調整前終値)'}
