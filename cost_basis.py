"""Reconstruct reference average costs from the registered history.

The source of purchase cost is actual settlement cash. Exact accounting inside
this module uses Decimal; output rounding is presentation only. Incomplete
history is never turned into an invented profit.
"""
from collections import defaultdict
from decimal import Decimal,ROUND_HALF_UP,localcontext
import re
import unicodedata
from ledger import parse_date
from custody_labels import trade_label,label

def money(value,places=2):
    if value is None:return ''
    unit=Decimal('1').scaleb(-places)
    value=value.quantize(unit,rounding=ROUND_HALF_UP)
    return format(value,'f').rstrip('0').rstrip('.') if '.' in format(value,'f') else format(value,'f')

def custody(value):
    return unicodedata.normalize('NFKC',value).replace(' ','')

def instrument_id(record):
    """Keep code-less mutual funds separate by their normalized names."""
    code=str(record.get('code','')).strip()
    if code:return code
    name=unicodedata.normalize('NFKC',str(record.get('name','')))
    return '名称:'+re.sub(r'\s+','',name)

def display_average(state):
    """Return the quote-unit average (per share, or per 10,000 fund units)."""
    if not state['valid'] or state['quantity']<=0:return None
    average=state['cost']/state['quantity']
    return average*Decimal(10_000) if state.get('asset_type')=='投資信託' else average

def calculate(store,activities,cutoff='9999-12-31'):
    """Return by-trade results and end positions. Labels always say 'reference'."""
    parse_date(cutoff)
    original=[t for t in store.records() if t['trade_date']<=cutoff]
    trades=[dict(t,custody=trade_label(t,original)) for t in original]
    splits=[e for e in activities.events() if e['type']=='株式分割' and e['date']<=cutoff]
    daily=defaultdict(set)
    for t in trades:
        daily[(t['account'],instrument_id(t),custody(t['custody']),t['trade_date'])].add(t['side'])
    timeline=[]
    for t in trades:timeline.append((t['trade_date'],1 if t['side']=='買' else 2,'trade',t))
    for s in splits:timeline.append((s['date'],0,'split',s))
    timeline.sort(key=lambda x:(x[0],x[1]))
    # Keys include custody so NISA and taxable stock are never combined.
    state=defaultdict(lambda:{'quantity':Decimal(0),'cost':Decimal(0),'valid':True,'reason':'',
        'name':'','code':'','custody':'','asset_type':'国内株式','quantity_unit':'株','price_unit':'円/株'})
    results={}
    def invalidate(s,reason):
        s['valid']=False;s['cost']=Decimal(0);s['reason']=reason
    with localcontext() as context:
        context.prec=40
        for day,_,kind,entry in timeline:
            account,code=entry['account'],entry['code']
            if kind=='split':
                split_key=('split',account,code,day)
                results[split_key]={'allocations':{}}
                allocations=entry.get('allocations')
                if allocations:
                    choices=allocations.items()
                else:
                    existing=[k[2] for k,v in state.items() if k[:2]==(account,code) and v['quantity']>0]
                    choices=[(existing[0],[entry['before'],entry['added'],entry['after']])] if len(existing)==1 else []
                if not choices:
                    if not any(k[:2]==(account,code) for k in state):
                        s=state[(account,code,'預り未確認')]
                        s.update(name=entry.get('name',''),code=code,custody='預り未確認',quantity=Decimal(entry['added']))
                        invalidate(s,'分割前の買付履歴・預り区分が不足')
                    for k,v in state.items():
                        if k[:2]==(account,code):invalidate(v,'分割の預り区分が不明')
                    continue
                for bucket,(before,added,after) in choices:
                    if not before and not added:continue
                    bucket=custody(bucket)
                    if bucket!='NISA':bucket=label(bucket,day)
                    if bucket=='NISA':
                        candidates=[k[2] for k,v in state.items() if k[:2]==(account,code) and 'NISA' in k[2] and v['quantity']>0]
                        if len(candidates)==1:bucket=candidates[0]
                        else:bucket='NISA(要確認)'
                    s=state[(account,code,bucket)]
                    s.update(name=entry.get('name',''),code=code,custody=custody(bucket),
                        asset_type='国内株式',quantity_unit='株',price_unit='円/株')
                    if s['quantity']!=Decimal(before):invalidate(s,'分割前の株数が通知と不一致（古い取引の不足など）')
                    s['quantity']+=Decimal(added)
                    results[split_key]['allocations'][bucket]=[before,added,after]
                continue
            t=entry
            if t.get('asset_type','国内株式') not in ('国内株式','投資信託'):
                results[t['record_id']]={'status':'対象外'}
                continue
            ident=instrument_id(t);bucket=custody(t['custody'])
            k=(account,ident,bucket);s=state[k]
            s.update(name=t.get('name',''),code=t.get('code',''),custody=bucket,
                asset_type=t.get('asset_type','国内株式'),quantity_unit=t.get('quantity_unit','株'),
                price_unit=t.get('price_unit','円/株'))
            qty=Decimal(t['quantity']);cash=Decimal(t['settlement_amount'])
            if daily[(account,ident,bucket,day)]=={'買','売'}:invalidate(s,'同日の売買順序が不明')
            if t['side']=='買':
                if s['quantity']==0 and not s['valid'] and daily[(account,ident,bucket,day)]!={'買','売'}:
                    s.update(valid=True,cost=Decimal(0),reason='')
                s['quantity']+=qty
                if s['valid']:s['cost']+=cash
                results[t['record_id']]={'status':'参考' if s['valid'] else '計算不能：'+s['reason'],
                    'quantity_after':s['quantity'],'cost_after':s['cost'] if s['valid'] else None,
                    'avg_after':display_average(s)}
                continue
            if s['quantity']<qty or s['quantity']<=0:invalidate(s,'売却前の買付履歴または開始残高が不足')
            if s['valid']:
                average=s['cost']/s['quantity']
                sold_cost=s['cost'] if s['quantity']==qty else s['cost']*qty/s['quantity']
                proceeds=cash
                s['cost']-=sold_cost
            else:average=sold_cost=proceeds=None
            s['quantity']-=qty
            results[t['record_id']]={'status':'参考' if s['valid'] else '計算不能：'+s['reason'],
                'average_before':average,'sale_cost':sold_cost,'proceeds':proceeds,
                'reference_gain':proceeds-sold_cost if s['valid'] else None,
                'quantity_after':s['quantity'],'cost_after':s['cost'] if s['valid'] else None,
                'avg_after':display_average(s)}
    # A dividend proves a security existed, but cannot reconstruct its cost or
    # its current shares when there are no trades at all for that account/code.
    for e in activities.events():
        if e['type']=='配当' and e['date']<=cutoff and e.get('code'):
            if not any(k[:2]==(e['account'],e['code']) for k in state):
                k=(e['account'],e['code'],'預り未確認');s=state[k]
                s.update(name=e['name'],code=e['code'],custody='預り未確認')
                invalidate(s,'配当のみ登録。株数・取得履歴が不足')
    return results,state
