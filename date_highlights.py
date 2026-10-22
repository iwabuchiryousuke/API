"""Upcoming dates only. Multi-date labels retain their source year."""
import re,calendar
from datetime import date,datetime,timedelta,timezone

def dates_in(value):
    year=None;result=[]
    for match in re.finditer(r'(?:(\d{4})[/-])?(\d{1,2})[/-](\d{1,2})',str(value)):
        if match[1]:year=int(match[1])
        if year is None:continue
        try:dt=date(year,int(match[2]),int(match[3]))
        except ValueError:continue
        result.append(dt)
    return sorted(set(result))
def months_after(day,n):
    month=day.month-1+n;year=day.year+month//12;month=month%12+1
    return date(year,month,min(day.day,calendar.monthrange(year,month)[1]))
def rules_for(cell,today=None):
    ds=dates_in(cell['v'])
    if not ds:return []
    today=today or datetime.now(timezone(timedelta(hours=9))).date()
    # Static base is intentionally unfilled, so expired conditional rules clear.
    cell['fill']=None
    def condition(n):
        parts=[f'AND(DATE({d.year},{d.month},{d.day})>=TODAY(),DATE({d.year},{d.month},{d.day})<=EDATE(TODAY(),{n}))' for d in ds]
        return 'OR('+','.join(parts)+')'
    red=condition(1);yellow='AND(NOT('+red+'),'+condition(2)+')'
    ref=('N' if cell['c']==14 else 'P')+str(cell['r'])
    return [{'range':ref,'formula':red,'dxf':0},{'range':ref,'formula':yellow,'dxf':1}]
