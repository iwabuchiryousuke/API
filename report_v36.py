"""Current acquisition + grouped financial rows; no historical cash inference.

The legacy report remains available for regression/migration checks. This
presentation does not rewrite the underlying PDF evidence or transaction rows.
"""
from datetime import date
import math
import unicodedata

from report_legacy import cutoff
from version import APP_VERSION


def apply_readable_layout(sheets):
    """Keep wrapped labels within their rows without moving the requested cells."""
    for sheet in sheets:
        if sheet['name']=='取引':
            widths={1:13,2:10,3:30,4:9,5:10,6:6,7:11,8:16,9:19,10:14,
                    11:12,12:16,13:16,14:3,15:16,16:14,17:14,18:18,19:18}
        elif sheet['name']=='配当':
            widths={1:13,2:10,3:30,4:9,5:10,6:8,7:11,8:16,9:19,10:3,
                    11:13,12:30,13:18,14:18,15:18,16:12,17:11,18:14,
                    19:16,20:18,21:18,22:18,23:42,24:18,25:18}
        else:continue
        sheet['widths'].update({str(column):width for column,width in widths.items()})
        sheet.update(header_height=38,row_height=24,freeze='C2')
        heights={}
        for cell in sheet['cells']:
            if cell['r']==1 or cell['k'].startswith('formula_') or not isinstance(cell['v'],str):continue
            if not cell.get('align',{}).get('wrap',True):continue
            capacity=max(4,float(sheet['widths'].get(str(cell['c']),14))-1.5)
            lines=0
            for line in cell['v'].split('\n'):
                units=sum(2 if unicodedata.east_asian_width(character) in ('W','F') else 1 for character in line)
                lines+=max(1,math.ceil(units/capacity))
            heights[cell['r']]=max(heights.get(cell['r'],24),min(409,lines*15+6))
        sheet['row_heights']=heights
    return sheets


def build(store,activities,month='',management=False,today=None):
    from current_capture import load_current_capture
    from trade_report import build_trade_sheet,GroupedStore
    from dividend_report import build_dividend_sheet
    # Validate the requested period before formatting either sheet. Current
    # captured cash is deliberately dated independently of this trade cutoff.
    trades=store.records(month);events=activities.events(month)
    as_of=cutoff(month)
    if not month:
        as_of=max([as_of]+[row['trade_date'] for row in trades]+[row['date'] for row in events])
    highlight_day=today or date.today()
    current=load_current_capture(store)
    grouped=GroupedStore(store)
    trade=build_trade_sheet(store,activities,as_of,current_capture=current,today=highlight_day)
    dividend=build_dividend_sheet(store,activities,as_of,today=highlight_day,trade_data=grouped)
    apply_readable_layout([trade,dividend])
    return dict(version=APP_VERSION,as_of=as_of,sheets=[trade,dividend],
                layout='current-capture-v3.6',management=bool(management))
