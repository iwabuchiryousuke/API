"""Headless desktop contracts and real isolated cash/PDF protection checks."""
from contextlib import ExitStack
import hashlib
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from pypdf import PdfWriter

from activity import Activities
from app import App,PRIMARY_ACTION,MANUAL_CASH_KINDS,add_manual_cash,archive_father_pdfs
from ledger import Store,LedgerError


class Variable:
    def __init__(self,master=None,value=None):self.value=value
    def get(self):return self.value
    def set(self,value):self.value=value


class FakeWidget(Mock):
    def __init__(self,*args,**kwargs):
        super().__init__()
        self.options=kwargs
        self.index=-1
    def current(self,index=None):
        if index is not None:self.index=index
        return self.index


class WidgetCapture:
    def __init__(self):self.widgets=[]
    def create(self,*args,**kwargs):
        widget=FakeWidget(*args,**kwargs);self.widgets.append(widget)
        widget.get_children.return_value=()
        return widget


class FocusedDesktop(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        self.store=Mock();self.store.accounts.return_value=[dict(id=identifier,label='Account '+identifier,owner_name='Owner '+identifier) for identifier in 'ABCDE']
        self.store.records.return_value=[]
        self.root=Mock();self.widgets=WidgetCapture();self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('app.BASE',self.base))
        self.stack.enter_context(patch('app.tk.StringVar',Variable))
        self.stack.enter_context(patch('app.tk.Toplevel',side_effect=self.widgets.create))
        self.stack.enter_context(patch('app.tk.Menu',side_effect=self.widgets.create))
        self.combos=self.stack.enter_context(patch('app.ttk.Combobox',side_effect=self.widgets.create))
        for name in ('Frame','Label','Button','Menubutton','Entry','Treeview','Scrollbar'):
            self.stack.enter_context(patch('app.ttk.'+name,side_effect=self.widgets.create))
        self.stack.enter_context(patch('app.ttk.Style'))
        self.stack.enter_context(patch('app.Store',return_value=self.store))
        self.stack.enter_context(patch('app.Activities'))
        self.stack.enter_context(patch('data_backup.backup_before_upgrade'))
        self.stack.enter_context(patch('family_profile.configure'))
        self.stack.enter_context(patch.dict(sys.modules,{'trade_report':SimpleNamespace(group_trades=Mock(return_value=[]))}))

    def texts(self):return [widget.options['text'] for widget in self.widgets.widgets if 'text' in widget.options]

    def test_primary_window_has_no_account_picker_or_retired_acquisition_actions(self):
        obj=App(self.root)
        self.combos.assert_not_called()
        labels=self.texts()
        for expected in (PRIMARY_ACTION,'父のPDF取り込み','税還・利息を手動で追加','Excelを出力'):
            self.assertEqual(labels.count(expected),1)
        for old in ('取り込み先の口座','選択口座のPDFだけ取得','保存したPDFのフォルダを開く','PDF取得結果一覧',
                    '分割を登録','取得した資産を確認','自動取得の予約・月次比較','配当・税還・利息を手動追加'):
            self.assertNotIn(old,labels)
        self.assertFalse(hasattr(obj,'account'))
        self.assertEqual(len(obj.accounts),5)

    def test_startup_retires_old_jobs_and_never_bootstraps_new_reservations(self):
        with patch('app.retire_background_tasks') as retire,patch('app.start_schedule_bootstrap') as bootstrap, \
             patch('initial_setup.ensure_personal'):
            App(self.root)
            delayed={call.args[0]:call.args[1] for call in self.root.after.call_args_list}
            delayed[800]()
        retire.assert_called_once_with(self.base)
        bootstrap.assert_not_called()

    def test_main_action_is_four_account_current_capture_without_selected_account(self):
        obj=object.__new__(App);obj.selected=Mock(side_effect=AssertionError('No account picker'))
        with patch('update_dialog.launch') as launch:obj.auto_collect()
        launch.assert_called_once_with(obj,self.base,online=True,current_only=True)
        obj.selected.assert_not_called()

    def test_manual_form_has_only_account_date_type_and_amount(self):
        obj=object.__new__(App);obj.root=self.root;obj.store=self.store;obj.busy=False
        obj.add_cash_entry()
        labels=self.texts()
        for expected in ('口座','日付（YYYY-MM-DD）','区分','金額（円）'):self.assertIn(expected,labels)
        for unwanted in ('内容','コード（配当）','預り（配当）','税額（配当）','株数（任意）','一株配当（任意）'):
            self.assertNotIn(unwanted,labels)
        choices=[call.kwargs.get('values') for call in self.combos.call_args_list]
        self.assertIn(MANUAL_CASH_KINDS,choices)
        self.assertFalse(any(isinstance(value,tuple) and '配当' in value for value in choices))
        self.assertEqual(len(choices),2)

    def test_father_import_uses_only_E_and_goes_to_current_offline_review(self):
        obj=object.__new__(App);obj.busy=False;obj.root=self.root;obj.store=self.store;obj.update_saved=Mock()
        obj.selected=Mock(side_effect=AssertionError('No account picker'))
        with patch('app.filedialog.askopenfilenames',return_value=('anonymous.pdf',)) as choose, \
             patch('app.archive_father_pdfs',return_value={'saved':1,'known':0,'errors':[]}) as archive:
            obj.import_father_pdfs()
        self.assertEqual(choose.call_args.kwargs['initialdir'],self.base/'data/電子ポストPDF/E')
        archive.assert_called_once_with(self.base,self.store,('anonymous.pdf',))
        obj.update_saved.assert_called_once();obj.selected.assert_not_called()
        with patch('update_dialog.launch') as launch:App.update_saved(obj)
        launch.assert_called_once_with(obj,self.base,online=False,current_only=True)

    def test_cancel_father_picker_does_not_start_review(self):
        obj=object.__new__(App);obj.busy=False;obj.root=self.root;obj.store=self.store;obj.update_saved=Mock()
        with patch('app.filedialog.askopenfilenames',return_value=()):obj.import_father_pdfs()
        obj.update_saved.assert_not_called()


class ManualCashProtection(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'data');self.addCleanup(self.store.close)
        self.activities=Activities(self.store)

    def test_tax_and_interest_keep_only_cash_fields_and_no_security_details(self):
        self.assertTrue(add_manual_cash(self.activities,'A','2026-10-29','税還','1,234.50'))
        self.assertTrue(add_manual_cash(self.activities,'E','2026-10-28','利息','9.25'))
        events=self.activities.events()
        self.assertEqual({event['type'] for event in events},{'税還付','利息'})
        self.assertEqual({event['account'] for event in events},{'A','E'})
        self.assertEqual({event['amount'] for event in events},{'1234.5','9.25'})
        for event in events:
            self.assertEqual(event['code'],'')
            for key in ('custody','quantity','per_share'):self.assertFalse(event.get(key))
            self.assertTrue(event['manual']);self.assertEqual(event['currency'],'JPY')
        self.assertEqual(self.store.records(),[])

    def test_dividend_or_bad_amount_never_creates_a_manual_event(self):
        for kind,amount in (('配当','1'),('税還付','1'),('税還','0'),('利息','-1'),('利息','NaN')):
            with self.subTest(kind=kind,amount=amount),self.assertRaises(LedgerError):
                add_manual_cash(self.activities,'A','2026-10-29',kind,amount)
        self.assertEqual(self.activities.events(),[])

    def test_duplicate_cash_entry_is_idempotent(self):
        self.assertTrue(add_manual_cash(self.activities,'A','2026-10-29','利息','10'))
        self.assertFalse(add_manual_cash(self.activities,'A','2026-10-29','利息','10.00'))
        self.assertEqual(len(self.activities.events()),1)

    def test_minimal_form_does_not_duplicate_existing_pdf_style_credit(self):
        self.activities.add_cash('A','税還付','2026-10-29','100','Existing PDF credit')
        before=self.activities.events()
        self.assertFalse(add_manual_cash(self.activities,'A','2026/10/29','税還','100.00'))
        self.assertEqual(self.activities.events(),before)


class FatherOriginalProtection(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.store=Store(self.base/'data');self.addCleanup(self.store.close)
        self.store.set_owner('E','Owner E');Activities(self.store)
        self.source=self.base/'father.pdf';writer=PdfWriter();writer.add_blank_page(200,200);writer.write(self.source)
        self.original=self.source.read_bytes()

    def test_scanned_valid_original_is_archived_only_in_E_without_financial_rows(self):
        result=archive_father_pdfs(self.base,self.store,[self.source])
        self.assertEqual(result,{'saved':1,'known':0,'errors':[]})
        self.assertEqual(self.source.read_bytes(),self.original)
        files=list((self.base/'data/電子ポストPDF/E').glob('*.pdf'))
        self.assertEqual(len(files),1);self.assertEqual(files[0].read_bytes(),self.original)
        self.assertEqual(self.store.records(),[]);self.assertEqual(Activities(self.store).events(),[])
        for identifier in 'ABCD':self.assertFalse((self.base/'data/電子ポストPDF'/identifier).exists())
        repeat=archive_father_pdfs(self.base,self.store,[self.source])
        self.assertEqual(repeat,{'saved':0,'known':1,'errors':[]})
        self.assertEqual(len(list((self.base/'data/電子ポストPDF/E').glob('*.pdf'))),1)

    def test_wrong_owner_never_pollutes_father_archive(self):
        digest=hashlib.sha256(self.original).hexdigest()
        for owner in ('Owner A','Owner Extra'):
            with self.subTest(owner=owner),patch('pdf_batch.classify',return_value=('trade',SimpleNamespace(owner=owner,digest=digest))):
                result=archive_father_pdfs(self.base,self.store,[self.source])
            self.assertEqual(result['saved'],0);self.assertEqual(len(result['errors']),1)
        self.assertFalse((self.base/'data/電子ポストPDF/E').exists())
        self.assertEqual(self.source.read_bytes(),self.original)

    def test_bad_original_is_reported_while_good_original_is_retained(self):
        bad=self.base/'bad.pdf';bad.write_bytes(b'not a PDF')
        result=archive_father_pdfs(self.base,self.store,[bad,self.source])
        self.assertEqual(result['saved'],1);self.assertEqual(len(result['errors']),1)
        self.assertEqual(result['errors'][0]['name'],'bad.pdf')
        self.assertEqual(bad.read_bytes(),b'not a PDF');self.assertEqual(self.source.read_bytes(),self.original)
        self.assertEqual(self.store.records(),[]);self.assertEqual(Activities(self.store).events(),[])

    def test_changed_document_after_validation_is_not_archived(self):
        digest=hashlib.sha256(self.original).hexdigest()
        def change(path):
            path.write_bytes(b'changed after validation')
            return 'trade',SimpleNamespace(owner='Owner E',digest=digest)
        with patch('pdf_batch.classify',side_effect=change):
            result=archive_father_pdfs(self.base,self.store,[self.source])
        self.assertEqual(result['saved'],0);self.assertEqual(len(result['errors']),1)
        self.assertFalse((self.base/'data/電子ポストPDF/E').exists())
