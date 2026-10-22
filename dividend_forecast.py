"""Calendar-year receipts, separate from the year of entitlement.
Return unknown on missing dates, units, custody or purchase/split history.
"""
from decimal import Decimal
from cost_basis import calculate

def forecast(store,activities,facts,as_of):
    year=int(as_of[:4]);out={};lines=[]
    for code,fact in facts.items():
        if not fact.get('calendar_complete'):out[code]=None;continue
        amount=Decimal(0);unknown=False
        for e in fact.get('events',[]):
            pay=e.get('payment');payyear=e.get('payment_year') or (int(pay[:4]) if pay else None)
            if payyear!=year:
                if payyear is None:unknown=True
                continue
            if not pay:unknown=True;continue
            if pay<=as_of:continue
            rights=e.get('rights');dps=e.get('dps')
            if not rights or dps is None:unknown=True;continue
            _,states=calculate(store,activities,min(rights,as_of))
            entries=[(k,s) for k,s in states.items() if s['code']==code]
            for k,s in entries:
                if not s['valid'] or s['quantity']<0 or '要確認' in k[2] or '未確認' in k[2]:unknown=True;continue
                if s['quantity']==0:continue
                # Only verified NISA has a known net rate here. Taxable net tax
                # rounding/distribution method require confirmation, not a guess.
                if k[2] not in ('NISA(成)','NISA(積)','旧NISA'):unknown=True;continue
                cash=s['quantity']*Decimal(str(dps));amount+=cash
                lines.append(dict(code=code,account=k[0],custody=k[2],rights=rights,payment=pay,quantity_date=min(rights,as_of),dps=dps,net_rate=1,estimated=rights>as_of))
        out[code]=None if unknown else float(amount)
    return out,lines
