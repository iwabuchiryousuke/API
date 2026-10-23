"""Fixed-JST calendar slots for read-only monthly acquisition.

The planner does not replay an old month's slot, shift a weekend, or turn a
test/late acquisition into a value observed at exactly 16:00.
"""
from calendar import monthrange
from datetime import datetime,timedelta,timezone
import argparse
import json
import math


JST=timezone(timedelta(hours=9),'JST')
ROLES=('auto','month-end','month-start')


def _aware(value):
    try:
        stamp=value if isinstance(value,datetime) else datetime.fromisoformat(value)
        if stamp.tzinfo is None or stamp.utcoffset() is None:raise ValueError()
        return stamp.astimezone(JST)
    except (ValueError,TypeError,OverflowError):
        raise ValueError('タイムゾーンを含む日時が必要です。') from None


def due_slot(now,role='auto',allow_early=False):
    """Return the slot only on its actual JST calendar boundary day.

    Production calls must keep allow_early=False. The explicit override exists
    for unit planning only; observation_timeliness still marks early data early.
    StartWhenAvailable cannot backdate a missed slot on another calendar day.
    """
    if role not in ROLES:raise ValueError('roleはauto/month-end/month-startです。')
    if not isinstance(allow_early,bool):raise ValueError('allow_earlyは真偽値で指定してください。')
    current=_aware(now)
    if current.day==1:actual='month-start'
    elif current.day==monthrange(current.year,current.month)[1]:actual='month-end'
    else:return None
    if role!='auto' and role!=actual:return None
    scheduled=current.replace(hour=16,minute=0,second=0,microsecond=0)
    if current<scheduled and not allow_early:return None
    if actual=='month-start':comparison=current.strftime('%Y-%m')
    elif current.month==12:comparison=f'{current.year+1:04d}-01'
    else:comparison=f'{current.year:04d}-{current.month+1:02d}'
    return {'role':actual,'scheduled_for':scheduled.isoformat(),
            'slot_date':current.date().isoformat(),'comparison_month':comparison,
            'include_pdfs':actual=='month-start'}


def observation_timeliness(scheduled_for,observed_at,tolerance_minutes=30):
    """Classify actual acquisition time without claiming an exact 16:00 value.

    on-time means an actual same-day observation in [16:00,16:30], not an
    assertion that the broker valued every component at precisely 16:00.
    """
    if isinstance(tolerance_minutes,bool) or not isinstance(tolerance_minutes,(int,float)) or not math.isfinite(tolerance_minutes) or tolerance_minutes<0:
        raise ValueError('許容時間は0以上の分数です。')
    scheduled=_aware(scheduled_for);observed=_aware(observed_at)
    if (scheduled.hour,scheduled.minute,scheduled.second,scheduled.microsecond)!=(16,0,0,0):
        raise ValueError('予定日時は日本時間16:00です。')
    if scheduled.day not in (1,monthrange(scheduled.year,scheduled.month)[1]):
        raise ValueError('予定日は暦月末または月初1日です。')
    delay=(observed-scheduled).total_seconds();same_day=scheduled.date()==observed.date()
    if not same_day:status='different-day'
    elif delay<0:status='early'
    elif delay<=tolerance_minutes*60:status='on-time'
    else:status='late'
    return {'status':status,'same_day':same_day,'within_16_window':status=='on-time',
            'delay_seconds':delay,'scheduled_for':scheduled.isoformat(),
            'observed_at':observed.isoformat(),'exact_16_claim':False}


def next_slots(now,count=2):
    """Preview upcoming native calendar slots, including an exact current slot."""
    if isinstance(count,bool) or not isinstance(count,int) or not 0<=count<=240:
        raise ValueError('表示件数は0～240の整数です。')
    current=_aware(now);result=[];year=current.year;month=current.month
    while len(result)<count:
        for day in (1,monthrange(year,month)[1]):
            planned=datetime(year,month,day,16,tzinfo=JST)
            if planned>=current:
                result.append(due_slot(planned))
                if len(result)==count:break
        if len(result)==count:break
        if month==12:
            if year==9999:raise ValueError('表示できる暦年の範囲を超えています。')
            year+=1;month=1
        else:month+=1
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description='日本時間の月末/月初16時スロットを表示します。処理は実行しません。')
    parser.add_argument('--role',choices=ROLES,default='auto')
    parser.add_argument('--now',help='タイムゾーン付きISO日時。省略時は現在時刻。')
    args=parser.parse_args(argv)
    try:
        now=_aware(args.now) if args.now else datetime.now(JST)
        print(json.dumps({'slot':due_slot(now,args.role),'planning_only':True},ensure_ascii=False,indent=2));return 0
    except ValueError as error:parser.error(str(error))


if __name__=='__main__':raise SystemExit(main())
