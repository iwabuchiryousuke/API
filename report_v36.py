"""Current acquisition + grouped financial rows; no historical cash inference.

The legacy report remains available for regression/migration checks. This
presentation does not rewrite the underlying PDF evidence or transaction rows.
"""
from datetime import datetime, timedelta, timezone

from report_legacy import cutoff
from version import APP_VERSION


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
    highlight_day=today or datetime.now(timezone(timedelta(hours=9))).date()
    current=load_current_capture(store)
    grouped=GroupedStore(store)
    trade=build_trade_sheet(store,activities,as_of,current_capture=current,today=highlight_day)
    dividend=build_dividend_sheet(store,activities,as_of,today=highlight_day,trade_data=grouped)
    from excel_template_v361 import apply_template_layout
    apply_template_layout([trade,dividend])
    return dict(version=APP_VERSION,as_of=as_of,sheets=[trade,dividend],
                layout='uploaded-template-v3.6.1',management=bool(management))
