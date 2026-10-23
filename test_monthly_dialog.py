import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import monthly_dialog as md
from monthly_records import save_measurement, comparison


class Checks(unittest.TestCase):
    def test_timestamps_amounts_and_unknown_are_distinct(self):
        self.assertEqual(md.display_stamp('2026-11-01T07:01:02+00:00'), '2026-11-01 16:01:02')
        self.assertEqual(md.display_stamp('2026-11-01T16:00:00'), '未取得・時刻未確認')
        self.assertNotIn('secret', md.display_stamp('https://secret/?token=secret'))
        self.assertEqual(md.amount(None),'未取得')
        self.assertEqual(md.amount(0),'0')
        self.assertEqual(md.amount(11,True),'+11')
        self.assertEqual(md.amount(-11,True),'-11')

    def test_coherent_comparison_rows_and_actual_dates(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            for role,day,stocks,cash in [('month-end','2026-10-31',100,0),('month-start','2026-11-01',115,None)]:
                slot=dict(role=role,slot_date=day,comparison_month='2026-11',scheduled_for=day+'T16:00:00+09:00',include_pdfs=role=='month-start')
                summary=dict(complete=cash is not None,missing=[] if cash is not None else ['cash_total'],values=dict(summary_stocks=stocks,cash_total=cash),observed_at=day+'T16:05:00+09:00',displayed_at=None)
                save_measurement(base,'A','架空太郎',slot,summary)
            value=comparison(base,'2026-11',{'A':'架空太郎'})
            rows=md.comparison_rows(value,{'A':{'label':'テスト口座'}})
            self.assertEqual(len(rows),4)
            self.assertEqual(len(rows[0]),12)
            self.assertIn('2026-10-31 16:05:00',rows[0])
            self.assertEqual(rows[0][4],'0')
            self.assertEqual(rows[0][7],'未取得')
            self.assertEqual(rows[0][8],'+15')
            self.assertEqual(rows[0][9],'未取得')
            self.assertEqual(rows[1][1],'PDF名義の設定が必要')

    def test_report_whitelist_and_attention_status(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);folder=base/'data'/'scheduler';folder.mkdir(parents=True)
            secret='https://example.invalid/?token=never-display-this'
            value=dict(status='partial',test=False,started_at='2026-11-01T16:00:00+09:00',finished_at='2026-11-01T16:06:00+09:00',error=secret,comparison_csv=secret,
                       accounts={'A':dict(asset_status='needs-attention',pdf_status='blocked',observed_at='2026-11-01T16:05:00+09:00',missing=['cash_total'],error=secret),
                                 'B':dict(asset_status='complete',pdf_status='complete',missing=[],password=secret),
                                 'C':dict(asset_status=secret,pdf_status=secret,observed_at=secret,missing=[secret])})
            (folder/'latest_run.json').write_text(json.dumps(value),encoding='utf-8')
            result=md.read_run_status(base)
            self.assertNotIn('never-display',str(result))
            self.assertIn('認証・設定の確認が必要',str(result))
            self.assertIn('認証・設定の確認待ち',str(result))
            self.assertIn('なし（両方取得）',str(result))
            self.assertIn('現金合計',str(result))
            self.assertEqual(len(result['rows']),4)
            self.assertIsNone(result['warning'])

    def test_bad_duplicate_oversized_and_link_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);folder=base/'data'/'scheduler';folder.mkdir(parents=True);path=folder/'latest_run.json'
            for raw in ['{bad','{"test":false,"test":true,"accounts":{}}','x'*(md._MAX_REPORT_BYTES+1)]:
                path.write_text(raw,encoding='utf-8')
                result=md.read_run_status(base)
                self.assertIsNotNone(result['warning']);self.assertEqual(result['rows'],[])
            path.unlink();other=base/'elsewhere.json';other.write_text('{}',encoding='utf-8')
            try:path.symlink_to(other)
            except (OSError,NotImplementedError):self.skipTest('Symlinks unavailable')
            self.assertIsNotNone(md.read_run_status(base)['warning'])

    def test_test_cannot_substitute_production_status(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);folder=base/'data'/'scheduler';folder.mkdir(parents=True)
            (folder/'latest_run.json').write_text(json.dumps(dict(test=True,status='completed',accounts={})),encoding='utf-8')
            self.assertIsNotNone(md.read_run_status(base)['warning'])
            self.assertEqual(md.read_run_status(base,True)['rows'],[])

    def fake_dialog(self):
        obj=md.MonthlyDialog.__new__(md.MonthlyDialog)
        button=MagicMock();button.cget.return_value='normal'
        combo=MagicMock();combo.cget.return_value='readonly'
        csv=MagicMock();csv.cget.return_value='disabled'
        app=SimpleNamespace(busy=False,buttons=[button],root=MagicMock())
        obj.app=app;obj.base=Path(md.__file__).resolve().parent;obj.controls=[combo,csv]
        obj.process=None;obj.saved_button_states=[];obj.dialog=MagicMock()
        obj.action_text=MagicMock();obj.logon=MagicMock();obj.logon.get.return_value='ログオン中（標準）'
        obj.refresh=MagicMock()
        return obj,button,combo,csv

    def test_process_sets_busy_polls_without_blocking_restores_prior_states(self):
        obj,button,combo,csv=self.fake_dialog();process=MagicMock();process.poll.side_effect=[None,0]
        with patch.object(md.os,'name','nt'),patch.object(md.subprocess,'Popen',return_value=process) as popen:
            obj.run_action('test')
        self.assertTrue(obj.app.busy)
        argv=popen.call_args.args[0]
        self.assertEqual(argv[0],sys.executable)
        self.assertEqual(argv[argv.index('--action')+1],'test')
        self.assertNotIn('--include-pdfs',argv)
        self.assertNotIn('shell',popen.call_args.kwargs)
        button.configure.assert_called_with(state='disabled')
        self.assertTrue(obj.app.root.after.called)
        obj.poll_action('テスト');self.assertTrue(obj.app.busy);obj.refresh.assert_not_called()
        obj.poll_action('テスト');self.assertFalse(obj.app.busy)
        button.configure.assert_called_with(state='normal')
        combo.configure.assert_called_with(state='readonly')
        csv.configure.assert_called_with(state='disabled')
        obj.refresh.assert_called_once()

    def test_spawn_failure_and_existing_busy_do_not_leave_lockout(self):
        obj,button,_,_=self.fake_dialog()
        with patch.object(md.os,'name','nt'),patch.object(md.subprocess,'Popen',side_effect=OSError('secret')):
            obj.run_action('test')
        self.assertFalse(obj.app.busy);self.assertIsNone(obj.process)
        self.assertNotIn('secret',str(obj.action_text.set.call_args))
        obj.app.busy=True
        with patch.object(md.subprocess,'Popen') as popen,patch.object(md.messagebox,'showinfo'):
            obj.run_action('test');popen.assert_not_called()

    def test_saved_csv_requires_fresh_current_owner_and_record_validation(self):
        obj,_,_,_=self.fake_dialog()
        obj.comparison_valid=True  # A previous month or previous owner map.
        obj.valid_month=MagicMock(return_value='2026-11')
        obj.refresh.side_effect=lambda:setattr(obj,'comparison_valid',False)
        with patch.object(md.webbrowser,'open') as browser,patch.object(md.messagebox,'showinfo') as info:
            obj.open_csv()
        obj.refresh.assert_called_once()
        browser.assert_not_called();info.assert_called_once()

    def test_launcher_passes_literal_paths_and_exit_status(self):
        with tempfile.TemporaryDirectory(prefix='monthly $() test ') as directory:
            path=Path(directory)/'tasks.py';output=Path(directory)/'args.json'
            path.write_text('import json,sys\nfrom pathlib import Path\nPath(__file__).with_name("args.json").write_text(json.dumps(sys.argv[1:]))\nraise SystemExit(7)\n',encoding='utf-8')
            result=subprocess.run([sys.executable,'-c',md._CONSOLE_LAUNCHER,str(path),'--action','status'],input='\n',text=True,capture_output=True)
            self.assertEqual(result.returncode,7)
            self.assertEqual(json.loads(output.read_text()),['--action','status'])
            self.assertIn('Enter',result.stdout)


if __name__=='__main__':unittest.main()
