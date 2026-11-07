"""Refresh public references and export using a worker-local ledger connection."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from activity import Activities
from ledger import Store
from standalone_excel import export_trades
from update_workflow import Cancelled

JST=timezone(timedelta(hours=9))


def export_with_research(base,month='',*,progress=lambda _:None,cancelled=lambda:False,
                         refresh=None,now=None):
    """Public reference failures never invent cash or modify financial records."""
    base=Path(base)
    if cancelled():raise Cancelled()
    store=Store(base/'data')
    try:
        records=store.records(month)
        activities=Activities(store)
        target=month or (now or datetime.now(JST)).astimezone(JST).strftime('%Y-%m')
        codes={row.get('code','') for row in records}
        codes.update(row.get('code','') for row in activities.events(month) if row.get('type')=='配当')
        if refresh is None:
            from market_data import refresh_public_research
            refresh=refresh_public_research
        progress('Yahooの終値とInvesting.comの今年の配当日を確認しています。')
        warning=False
        try:
            result=refresh(store.directory,codes,target,cancelled=cancelled)
            warning=bool(isinstance(result,dict) and result.get('failed'))
        except Cancelled:raise
        except Exception:
            warning=True
        if cancelled():raise Cancelled()
        progress('添付見本の書式でExcelを出力しています。')
        path=export_trades(store,base/'output',month,activities)
        return {'output':path,'public_research_incomplete':warning}
    finally:
        store.close()
