"""Dated public research + optional JPX official closing-price API.
Private API credentials are stored only via Windows DPAPI, never here.
"""
import json,calendar,os,re,tempfile,math,time
from pathlib import Path
from datetime import date,datetime,timedelta,timezone
from urllib.request import Request,urlopen
from urllib.parse import urlencode

def load_research(directory,year):
    path=Path(directory)/'market'/f'research_{year}.json'
    if not path.exists():path=Path(__file__).parent/'reference'/f'research_{year}.json'
    if not path.exists():return {'year':year,'securities':{}}
    try:
        if path.is_symlink() or path.stat().st_size>4*1024*1024:raise ValueError()
        def unique(pairs):
            result={}
            for key,item in pairs:
                if key in result:raise ValueError()
                result[key]=item
            return result
        value=json.loads(path.read_text(encoding='utf-8'),object_pairs_hook=unique)
        if (not isinstance(value,dict) or type(value.get('year')) is not int or value['year']!=year
                or not isinstance(value.get('securities'),dict)
                or any(not isinstance(k,str) or not isinstance(v,dict) for k,v in value['securities'].items())):
            raise ValueError()
        def finite_tree(item):
            if isinstance(item,float) and not math.isfinite(item):raise ValueError()
            if isinstance(item,dict):
                for child in item.values():finite_tree(child)
            elif isinstance(item,list):
                for child in item:finite_tree(child)
        def amount(item):
            if item in (None,''):return
            if isinstance(item,bool) or not isinstance(item,(str,int,float)):raise ValueError()
            number=float(item)
            if not math.isfinite(number) or number<0:raise ValueError()
        def iso_day(item):
            if item in (None,''):return
            if not isinstance(item,str) or date.fromisoformat(item).isoformat()!=item:raise ValueError()
        finite_tree(value)
        for fact in value['securities'].values():
            for key in ('name','rights','payments','earnings','status','source'):
                if fact.get(key) is not None and not isinstance(fact[key],str):raise ValueError()
            for key in ('dps','close','month_start_close'):amount(fact.get(key))
            for key in ('close_date','month_start_close_date'):iso_day(fact.get(key))
            if fact.get('calendar_complete') is not None and type(fact['calendar_complete']) is not bool:raise ValueError()
            if not isinstance(fact.get('events',[]),list):raise ValueError()
            for event in fact.get('events',[]):
                if not isinstance(event,dict):raise ValueError()
                for key in ('rights','payment','record_date','ex_date'):iso_day(event.get(key))
                amount(event.get('dps'))
                if event.get('payment_year') is not None and (type(event['payment_year']) is not int or not 1900<=event['payment_year']<=9999):raise ValueError()
        value.pop('_load_error',None)
        return value
    except (OSError,ValueError,TypeError,UnicodeError,RecursionError,OverflowError):
        # Public reference failures must not discard valid financial records.
        # Preserve the original file for a manual repair, and display blanks.
        return {'year':year,'securities':{},'_load_error':True}


def refresh_month_closes(directory,codes,month,provider=None,cancelled=lambda:False,budget_seconds=25,now=None):
    """Fill missing dated public prices; caller supplies an isolated stage dir.

    Existing verified dates, dividend plans and manual facts remain intact.
    A missing first-day trading bar is never shifted to the following day.
    """
    first=date.fromisoformat(month+'-01');year=first.year
    if first.strftime('%Y-%m')!=month:raise ValueError('対象月をYYYY-MMで指定してください。')
    codes=sorted({str(code) for code in codes if re.fullmatch(r'[0-9][0-9A-Z]{3}',str(code))})
    if cancelled():return {'updated':0,'failed':codes,'written':False,'cancelled':True}
    jst=timezone(timedelta(hours=9));moment=now or datetime.now(jst)
    if moment.tzinfo is None:raise ValueError('現在時刻にタイムゾーンを指定してください。')
    moment=moment.astimezone(jst)
    if first>moment.date():return {'updated':0,'failed':codes,'written':False}
    first_ready=first<moment.date() or (moment.hour,moment.minute)>=(16,0)
    data=load_research(directory,year)
    if data.get('_load_error'):return {'updated':0,'failed':codes,'written':False}
    from yahoo_market import month_closes
    provider=provider or month_closes
    facts=data['securities'];previous=first-timedelta(days=1);targets={}
    def known(fact,key,date_key,prev=False):
        try:
            if isinstance(fact[key],bool):return False
            value=float(fact[key]);stamp=date.fromisoformat(fact[date_key])
            if not math.isfinite(value) or value<=0:return False
            return (stamp.year==previous.year and stamp.month==previous.month and
                    0<=(previous-stamp).days<=7) if prev else first_ready and stamp==first
        except (KeyError,TypeError,ValueError,OverflowError):return False
    for code in codes:
        fact=facts.get(code,{})
        need=(not known(fact,'close','close_date',True),first_ready and not known(fact,'month_start_close','month_start_close_date'))
        if any(need):targets[code]=need
    if not targets:return {'updated':0,'failed':[],'written':False}
    if isinstance(budget_seconds,bool) or not isinstance(budget_seconds,(int,float)) or not math.isfinite(budget_seconds) or budget_seconds<=0:
        raise ValueError('公開情報取得の待ち時間を確認してください。')
    from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
    received={};failed=[]
    pool=ThreadPoolExecutor(max_workers=4);deadline=time.monotonic()+budget_seconds
    try:
        pending={pool.submit(provider,code,month):code for code in targets}
        while pending:
            if cancelled():return {'updated':0,'failed':sorted(targets),'written':False,'cancelled':True}
            remaining=deadline-time.monotonic()
            if remaining<=0:
                failed.extend(pending.values());break
            done,_=wait(pending,timeout=min(.2,remaining),return_when=FIRST_COMPLETED)
            for future in done:
                code=pending.pop(future)
                try:
                    value=future.result()
                    if not isinstance(value,dict):raise ValueError()
                    received[code]=value
                except Exception:failed.append(code)
    finally:
        for future in pending:future.cancel()
        pool.shutdown(wait=False,cancel_futures=True)
    if cancelled():return {'updated':0,'failed':sorted(targets),'written':False,'cancelled':True}
    updated=0
    for code,value in received.items():
        old=dict(facts.get(code,{}));changed=False
        for needed,key,date_key,source_key,prev in (
                (targets[code][0],'close','close_date','price_source',True),
                (targets[code][1],'month_start_close','month_start_close_date','month_start_price_source',False)):
            if needed and known(value,key,date_key,prev):
                old[key]=value[key];old[date_key]=value[date_key]
                if isinstance(value.get(source_key),str):old[source_key]=value[source_key]
                changed=True
        if changed:facts[code]=old;updated+=1
        elif code not in failed:failed.append(code)
    if not updated:return {'updated':0,'failed':sorted(failed),'written':False}
    folder=Path(directory)/'market';folder.mkdir(parents=True,exist_ok=True)
    target=folder/f'research_{year}.json'
    if folder.is_symlink() or target.is_symlink():raise ValueError('公開参考情報の通常の保存先を確認してください。')
    descriptor,temporary=tempfile.mkstemp(prefix='.research_',suffix='.tmp',dir=folder)
    try:
        with os.fdopen(descriptor,'w',encoding='utf-8') as stream:
            json.dump(data,stream,ensure_ascii=False,indent=2,allow_nan=False);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,target)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)
    return {'updated':updated,'failed':sorted(failed),'written':True}

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
