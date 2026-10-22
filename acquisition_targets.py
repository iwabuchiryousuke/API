"""Use the report's month for acquisition, without substituting current values."""
from datetime import date,timedelta


def target_dates(month='',today=None):
    if month:
        from report_model import cutoff
        day=date.fromisoformat(cutoff(month))
    else:
        if today is None:
            from sbi_views import _jst_now
            today=_jst_now().date()
        day=today
    first=day.replace(day=1)
    return ((first-timedelta(days=1)).isoformat(),first.isoformat())
