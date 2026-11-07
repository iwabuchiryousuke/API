"""Visible desktop placement and durable, nonfinancial dividend ordering."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from app import App
from dividend_order import (FILENAME,load,save,ordered_codes,move_codes,
                            security_choices)
from ledger import LedgerError,Store
import test_app_v36 as desktop_helpers


class DesktopPlacement(unittest.TestCase):
    def setUp(self):
        self.fixture=desktop_helpers.FocusedDesktop('test_main_action_is_four_account_current_capture_without_selected_account')
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.root=self.fixture.root
        for name in ('Frame','Label','Button','Menubutton','Entry','Treeview','Scrollbar'):
            self.fixture.stack.enter_context(patch('app.ttk.'+name,side_effect=self.create))

    def create(self,*args,**kwargs):
        widget=self.fixture.widgets.create(*args,**kwargs)
        widget.parent_widget=args[0] if args else None
        return widget

    def test_settings_contains_exact_five_account_and_authentication_actions(self):
        obj=App(self.root)
        menu=obj.settings.configure.call_args.kwargs['menu']
        self.assertEqual([call.kwargs['label'] for call in menu.add_command.call_args_list],
                         ['口座名を変更','PDF名義を確認・変更','SBIログイン情報','Gmail連携を設定','Gmailだけ診断'])
        menu.add_separator.assert_not_called()

    def test_list_actions_are_visible_together_above_the_transaction_table(self):
        obj=App(self.root);widgets=self.fixture.widgets.widgets
        labels={widget.options.get('text'):widget for widget in widgets if 'text' in widget.options}
        parent=labels['保存PDFを反映'].parent_widget
        expected=(('保存PDFを反映','update_saved'),('銘柄設定','set_security_names'),
                  ('出力フォルダを開く','open_outputs'))
        for label,method in expected:
            button=labels[label]
            self.assertIs(button.parent_widget,parent)
            self.assertEqual(button.options['command'].__name__,method)
            self.assertLess(widgets.index(button),widgets.index(obj.tree))
            self.assertIn(button,obj.buttons)
        for removed in ('診断ZIPを作成','説明書を開く','配当予定・終値を確認'):
            self.assertNotIn(removed,labels)

    def test_stock_settings_open_the_combined_name_and_order_editor(self):
        obj=object.__new__(App);obj.busy=False;obj.root=self.root
        obj.store=Mock();obj.activities=Mock()
        with patch('dividend_order.edit') as editor:
            obj.set_security_names()
        editor.assert_called_once_with(self.root,obj.store,obj.activities)

    def test_acquisition_in_progress_does_not_open_stock_settings(self):
        obj=object.__new__(App);obj.busy=True
        with patch('dividend_order.edit') as editor:obj.set_security_names()
        editor.assert_not_called()


class DividendOrderProtection(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.base=Path(self.temporary.name);self.directory=self.base/'data'

    def test_missing_preference_is_read_only_and_all_present_codes_are_retained(self):
        self.assertEqual(load(self.directory),[])
        self.assertEqual(ordered_codes(self.directory,['314A','6501','F-ABCDEF0123','未分類']),
                         ['314A','6501','F-ABCDEF0123','未分類'])
        self.assertFalse(self.directory.exists())

    def test_order_survives_restart_and_future_codes_append_without_losing_saved_preferences(self):
        save(self.directory,['6146','6501','314A'])
        self.assertEqual(load(self.directory),['6146','6501','314A'])
        self.assertEqual(ordered_codes(self.directory,['314A','9000','6501']),['6501','314A','9000'])
        self.assertEqual(load(self.directory),['6146','6501','314A'])
        self.assertEqual(ordered_codes(self.directory,['9000','6146','6501','314A']),
                         ['6146','6501','314A','9000'])

    def test_saving_order_does_not_modify_any_financial_or_private_file(self):
        store=Store(self.directory)
        with store.db:store.db.execute('CREATE TABLE opaque_user_table (value TEXT)')
        store.close()
        protected={
            self.directory/'private/login.secure':b'opaque synthetic encrypted bytes',
            self.directory/'電子ポストPDF/A/synthetic.pdf':b'%PDF-synthetic-original-bytes',
            self.directory/'security_names.json':b'{"6501":"Synthetic name"}',
        }
        for path,value in protected.items():path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(value)
        protected[self.directory/'ledger.sqlite3']=(self.directory/'ledger.sqlite3').read_bytes()
        before={str(path):hashlib.sha256(raw).hexdigest() for path,raw in protected.items()}
        save(self.directory,['6501','6146']);ordered_codes(self.directory,['6501','314A','6146'])
        self.assertEqual({str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in protected},before)
        self.assertEqual(json.loads((self.directory/FILENAME).read_text()),{'schema':1,'codes':['6501','6146']})

    def test_invalid_or_duplicate_order_cannot_replace_a_valid_preference(self):
        save(self.directory,['6501']);before=(self.directory/FILENAME).read_bytes()
        for values in (['6501','６５０１'],[''],['6501','bad\ncode'],[True],['x'*101],None,'6501'):
            with self.subTest(values=values),self.assertRaises(LedgerError):save(self.directory,values)
            self.assertEqual((self.directory/FILENAME).read_bytes(),before)

    def test_interrupted_atomic_replace_keeps_original_order_and_cleans_temporary_file(self):
        save(self.directory,['6501']);before=(self.directory/FILENAME).read_bytes()
        with patch('dividend_order.os.replace',side_effect=OSError('synthetic interrupted save')):
            with self.assertRaises(OSError):save(self.directory,['6146','6501'])
        self.assertEqual((self.directory/FILENAME).read_bytes(),before)
        self.assertEqual([path.name for path in self.directory.iterdir()],[FILENAME])

    def test_corrupt_or_unknown_schema_is_not_silently_overwritten_during_read(self):
        self.directory.mkdir()
        for raw in (b'invalid JSON',b'{"schema":true,"codes":[]}',
                    b'{"schema":2,"codes":[]}',b'{"schema":1,"codes":[],"codes":["6501"]}'):
            path=self.directory/FILENAME;path.write_bytes(raw)
            with self.subTest(raw=raw),self.assertRaises(LedgerError):load(self.directory)
            self.assertEqual(path.read_bytes(),raw)

    def test_preference_symlink_never_reads_or_replaces_its_target(self):
        self.directory.mkdir();target=self.base/'target.json';target.write_text('{"schema":1,"codes":["6501"]}')
        link=self.directory/FILENAME
        try:link.symlink_to(target)
        except OSError:self.skipTest('Symlinks unavailable on this host')
        before=target.read_bytes()
        with self.assertRaises(LedgerError):load(self.directory)
        with self.assertRaises(LedgerError):save(self.directory,['6146'])
        self.assertTrue(link.is_symlink());self.assertEqual(target.read_bytes(),before)

    def test_move_controls_preserve_other_rows_and_never_mutate_original_sequence(self):
        original=['6501','6146','314A']
        self.assertEqual(move_codes(original,'6146',-1),['6146','6501','314A'])
        self.assertEqual(move_codes(original,'6146',1),['6501','314A','6146'])
        self.assertEqual(move_codes(original,'6501',-1),original)
        self.assertEqual(move_codes(original,'314A',1),original)
        self.assertEqual(move_codes(original,'9000',1),original)
        self.assertEqual(original,['6501','6146','314A'])

    def test_editor_includes_receipt_only_securities_and_uses_registered_display_names(self):
        store=SimpleNamespace(directory=self.directory,records=lambda:[{'code':'6501','name':'Original name'}])
        activities=SimpleNamespace(events=lambda:[{'type':'配当','code':'6146','name':'Receipt name'},
                                                  {'type':'配当','code':'','name':''},
                                                  {'type':'利息','code':'9000','name':'Not a security'}])
        with patch('security_names.load',return_value={'6501':'Registered name','314A':'Future security'}):
            names=security_choices(store,activities)
        self.assertEqual(names,{'6501':'Registered name','6146':'Receipt name','未分類':'コード未登録の配当','314A':'Future security'})

    def test_editor_canonicalizes_lowercase_and_fullwidth_alpha_codes_without_rewriting_records(self):
        rows=[{'code':'314a','name':'Original lower-case name'}]
        store=SimpleNamespace(directory=self.directory,records=lambda:rows)
        activities=SimpleNamespace(events=lambda:[{'type':'配当','code':'３１４ａ','name':'Receipt spelling'}])
        with patch('security_names.load',return_value={'314A':'Registered name'}):
            names=security_choices(store,activities)
        self.assertEqual(names,{'314A':'Registered name'})
        self.assertEqual(ordered_codes(self.directory,names),['314A'])
        self.assertEqual([names[key] for key in ordered_codes(self.directory,names)],['Registered name'])
        self.assertEqual(rows[0]['code'],'314a')


if __name__=='__main__':unittest.main()
