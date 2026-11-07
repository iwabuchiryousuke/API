"""The compact all-period desktop and its shared upcoming calendar."""
from datetime import date,datetime,timezone,timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock,patch
import unittest

from app import App,PRIMARY_ACTION,calendar_rows
from ledger import LedgerError
import test_app_v36 as desktop_helpers


JST=timezone(timedelta(hours=9))


class FixedDateTime(datetime):
    @classmethod
    def now(cls,tz=None):return cls(2026,11,7,12,0,tzinfo=JST)


class CompactDesktop(unittest.TestCase):
    def setUp(self):
        self.fixture=desktop_helpers.FocusedDesktop('test_main_action_is_four_account_current_capture_without_selected_account')
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.root=self.fixture.root
        for name in ('Frame','Label','Button','Menubutton','Entry','Treeview','Scrollbar','Panedwindow'):
            self.fixture.stack.enter_context(patch('app.ttk.'+name,side_effect=self.create))

    def create(self,*args,**kwargs):
        widget=self.fixture.widgets.create(*args,**kwargs)
        widget.parent_widget=args[0] if args else None
        return widget

    def test_five_actions_share_one_horizontal_row_in_the_requested_order(self):
        obj=App(self.root)
        labels=('保存PDFを反映','銘柄設定','出力フォルダを開く','父のPDF取り込み','税還・利息を手動で追加')
        widgets=self.fixture.widgets.widgets
        selected=[widget for widget in widgets if widget.options.get('text') in labels]
        self.assertEqual([widget.options['text'] for widget in selected],list(labels))
        self.assertEqual(len({id(widget.parent_widget) for widget in selected}),1)
        for widget in selected:
            self.assertEqual(widget.pack.call_args.kwargs['side'],'left')
            self.assertIn(widget,obj.buttons)
        commands=[widget.options['command'].__name__ for widget in selected]
        self.assertEqual(commands,['update_saved','set_security_names','open_outputs','import_father_pdfs','add_cash_entry'])

    def test_automatic_acquisition_is_compact_and_month_selection_is_absent(self):
        obj=App(self.root);widgets=self.fixture.widgets.widgets
        self.assertEqual(PRIMARY_ACTION,'全データ自動取得')
        primary=next(widget for widget in widgets if widget.options.get('text')==PRIMARY_ACTION)
        self.assertEqual(primary.options['command'].__name__,'auto_collect')
        primary.pack.assert_not_called()
        self.assertEqual(primary.grid.call_args.kwargs['sticky'],'w')
        self.assertFalse(hasattr(obj,'month'))
        self.assertNotIn('出力対象月',self.fixture.texts())
        self.assertNotIn('YYYY-MM／空欄はすべて',self.fixture.texts())

    def test_transactions_and_calendar_have_independent_tables_in_resizable_panes(self):
        obj=App(self.root);widgets=self.fixture.widgets.widgets
        panes=[widget for widget in widgets if 'orient' in widget.options and widget.options['orient']=='horizontal' and widget.add.call_count]
        self.assertEqual(len(panes),1)
        self.assertEqual([call.kwargs['weight'] for call in panes[0].add.call_args_list],[3,2])
        self.assertIsNot(obj.tree,obj.calendar)
        self.assertEqual(obj.calendar.options['columns'],('date','name','kind','amount'))
        obj.calendar.tag_configure.assert_called_once_with('within_month',background='#ff7c80')
        self.assertEqual([call.kwargs['text'] for call in obj.calendar.heading.call_args_list],['日付','銘柄','予定','配当金額'])


class UpcomingCalendarView(unittest.TestCase):
    def setUp(self):
        self.obj=object.__new__(App);self.obj.store=Mock();self.obj.activities=Mock()
        self.obj.calendar=Mock();self.obj.calendar.get_children.return_value=('previous',)
        self.obj.calendar_status=desktop_helpers.Variable()
        self.clock=patch('app.datetime',FixedDateTime);self.clock.start();self.addCleanup(self.clock.stop)

    def test_calendar_shows_future_first_and_numeric_dividend_amounts_without_status_suffix(self):
        rows=[dict(date='2026-12-20',name='Later',code='9002',kind='権利付き最終日',amount=100,within_month=False),
              dict(date='2026-11-06',name='Past',code='9000',kind='配当金支払日',amount=1,within_month=False),
              dict(date='2026-11-15',name='Next',code='9001',kind='本決算発表日',amount=None,within_month=True),
              dict(date='2026-11-07',name='Today',code='9000',kind='配当金支払日',amount=Decimal('1234.5'),amount_kind='予想',within_month=True)]
        with patch('app.calendar_rows',return_value=rows) as schedule:self.obj.refresh_calendar()
        schedule.assert_called_once_with(self.obj.store,self.obj.activities,today=date(2026,11,7))
        self.obj.calendar.delete.assert_called_once_with('previous')
        inserted=self.obj.calendar.insert.call_args_list
        self.assertEqual([call.kwargs['values'] for call in inserted],
                         [('11/7','Today','配当金支払日','1,234.5'),
                          ('11/15','Next','本決算発表日',''),('12/20','Later','権利付き最終日','')])
        self.assertEqual([call.kwargs['tags'] for call in inserted],[('within_month',),('within_month',),()])
        self.assertIn('1か月以内',self.obj.calendar_status.get())

    def test_explicit_zero_and_unconfirmed_amount_remain_distinct(self):
        rows=[dict(date='2026-11-08',name='Zero',code='9000',kind='配当金支払日',amount=0,within_month=True),
              dict(date='2026-11-09',name='Unknown',code='9001',kind='配当金支払日',amount=None,within_month=True)]
        with patch('app.calendar_rows',return_value=rows):self.obj.refresh_calendar()
        self.assertEqual([call.kwargs['values'][-1] for call in self.obj.calendar.insert.call_args_list],['0',''])

    def test_unavailable_calendar_does_not_raise_or_show_stale_events(self):
        with patch('app.calendar_rows',side_effect=LedgerError('synthetic invalid research')):
            self.obj.refresh_calendar()
        self.obj.calendar.delete.assert_called_once_with('previous')
        self.obj.calendar.insert.assert_not_called()
        self.assertIn('予定を表示できません',self.obj.calendar_status.get())

    def test_local_periodic_refresh_skips_busy_work_and_remains_scheduled(self):
        self.obj.root=Mock();self.obj.refresh_calendar=Mock();self.obj.busy=True
        self.obj.refresh_calendar_periodic();self.obj.refresh_calendar.assert_not_called()
        self.obj.busy=False;self.obj.refresh_calendar_periodic();self.obj.refresh_calendar.assert_called_once_with()
        self.assertEqual([call.args[0] for call in self.obj.root.after.call_args_list],[60000,60000])

    def test_shared_schedule_is_requested_as_upcoming_only_without_an_independent_filter(self):
        # The helper is patched by the shared legacy fixture in other test
        # classes, but this class verifies the actual adapter to the common API.
        import app
        with patch('upcoming_calendar.schedule_rows',return_value=[]) as schedule:
            self.assertEqual(app.calendar_rows(self.obj.store,self.obj.activities,today=date(2026,11,7)),[])
        schedule.assert_called_once_with(self.obj.store,self.obj.activities,today=date(2026,11,7),upcoming_only=True)


class AllPeriodExport(unittest.TestCase):
    def test_export_has_no_dependency_on_a_removed_month_widget(self):
        fixture=desktop_helpers.FocusedDesktop('test_main_action_is_four_account_current_capture_without_selected_account')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        obj=object.__new__(App);obj.busy=False;obj.store=Mock();obj.root=fixture.root
        obj.buttons=[Mock()];obj.refresh_calendar=Mock()
        class ImmediateThread:
            def __init__(self,target,daemon=False):self.target=target
            def start(self):self.target()
        result={'output':Path('synthetic.xlsx'),'public_research_incomplete':False}
        with (patch('threading.Thread',ImmediateThread),
              patch('export_workflow.export_with_research',return_value=result) as export,
              patch('app.messagebox.showinfo') as notification):
            obj.export_excel()
        obj.store.records.assert_called_once_with('')
        self.assertEqual(export.call_args.args,(fixture.base,''))
        self.assertTrue(callable(export.call_args.kwargs['cancelled']))
        obj.refresh_calendar.assert_called_once_with();notification.assert_called_once()
        self.assertFalse(obj.busy)


if __name__=='__main__':unittest.main()
