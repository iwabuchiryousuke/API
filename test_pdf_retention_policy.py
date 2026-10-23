"""Valid originals can remain unparsed without blocking other verified data."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pypdf import PdfReader, PdfWriter

from ledger import LedgerError, Snapshot, Store
from pdf_batch import ArchivedUnparsedDocument, KnownDocumentExclusion, classify
from sbi_views import init_observations, save_observation
from test_data_compatibility import seed_fixture
from test_document_classification import japanese_pdf
from update_workflow import Cancelled, UpdateError, Workflow, fingerprint



class RetainedPDFClassification(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)

    def test_actual_cumulative_basis_title_is_nontransaction_and_retained(self):
        path=self.folder/'arbitrary.pdf'
        japanese_pdf(path,'特 定 累 積 投 資 勘 定 基 準 額 等 通 知 書')
        original=path.read_bytes()
        with self.assertRaises(KnownDocumentExclusion) as raised:classify(path)
        self.assertEqual(raised.exception.code,'CUMULATIVE_NISA_BASIS')
        self.assertFalse(raised.exception.needs_review)
        self.assertIn('売買約定・配当入金',raised.exception.reason)
        self.assertEqual(path.read_bytes(),original)

    def test_unknown_valid_pdf_has_explicit_unparsed_metadata(self):
        path=self.folder/'unknown.pdf';japanese_pdf(path,'未知の重要なお知らせ')
        original=path.read_bytes()
        with self.assertRaises(ArchivedUnparsedDocument) as raised:classify(path)
        self.assertEqual(raised.exception.code,'UNPARSED_PDF')
        self.assertTrue(raised.exception.needs_review)
        self.assertIn('金額・取引は集計しません',str(raised.exception))
        self.assertEqual(path.read_bytes(),original)

    def test_valid_image_only_pdf_is_retained_without_invented_text(self):
        path=self.folder/'scan.pdf';writer=PdfWriter();writer.add_blank_page(200,200);writer.write(path)
        original=path.read_bytes()
        with self.assertRaises(ArchivedUnparsedDocument) as raised:classify(path)
        self.assertEqual(raised.exception.code,'UNREADABLE_VALID_PDF')
        self.assertIsNone(raised.exception.owner)
        self.assertEqual(path.read_bytes(),original)

    def test_valid_original_over_parser_page_limit_is_retained(self):
        path=self.folder/'long.pdf';writer=PdfWriter()
        for _ in range(51):writer.add_blank_page(200,200)
        writer.write(path)
        with self.assertRaises(ArchivedUnparsedDocument) as raised:classify(path)
        self.assertEqual(raised.exception.code,'UNREADABLE_VALID_PDF')
        self.assertEqual(len(PdfReader(path).pages),51)

    def test_recognized_trade_layout_failure_becomes_unparsed_not_a_fake_trade(self):
        path=self.folder/'trade.pdf';japanese_pdf(path,'株式取引報告書')
        with patch('pdf_batch.parse_report',side_effect=LedgerError('未検証の数値配置')):
            with self.assertRaises(ArchivedUnparsedDocument) as raised:classify(path)
        self.assertEqual(raised.exception.code,'PARSER_UNCONFIRMED')
        self.assertIn('未検証の数値配置',raised.exception.reason)

    def test_recognized_parser_value_error_is_retained_but_runtime_bug_propagates(self):
        path=self.folder/'trade.pdf';japanese_pdf(path,'株式取引報告書')
        with patch('pdf_batch.parse_report',side_effect=ValueError('invalid value')):
            with self.assertRaises(ArchivedUnparsedDocument):classify(path)
        with patch('pdf_batch.parse_report',side_effect=RuntimeError('implementation bug')):
            with self.assertRaises(RuntimeError):classify(path)

    def test_positive_cross_page_owner_conflict_remains_critical(self):
        page=SimpleNamespace(best_compact='A501取引報告書',compact='A501取引報告書',
                             best_text='Example A 様',text='Example A 様')
        data=SimpleNamespace(pages=[page],best_compact=page.best_compact,compact=page.compact)
        with patch('pdf_batch.load_pdf',return_value=data), \
             patch('smbc_import.parse_smbc',side_effect=LedgerError('SMBCのページ間で名義が一致しません。')):
            with self.assertRaises(LedgerError) as raised:classify(self.folder/'unused.pdf')
        self.assertNotIsInstance(raised.exception,ArchivedUnparsedDocument)

    def test_invalid_or_truncated_files_are_never_unparsed_success(self):
        for raw in (b'not PDF',b'%PDF-1.7\ntruncated',b'<html>login</html>'):
            path=self.folder/'bad.pdf';path.write_bytes(raw)
            with self.subTest(raw=raw),self.assertRaises(LedgerError) as raised:classify(path)
            self.assertNotIsInstance(raised.exception,ArchivedUnparsedDocument)
            self.assertEqual(path.read_bytes(),raw)

    def test_missing_pdf_dependency_still_requires_setup(self):
        with patch('pdf_batch.load_pdf',side_effect=LedgerError('PDF機能の準備が必要です。')):
            with self.assertRaises(LedgerError) as raised:classify('unused.pdf')
        self.assertNotIsInstance(raised.exception,ArchivedUnparsedDocument)

    def test_unparsed_metadata_preserves_a_readable_owner_for_account_checks(self):
        page=SimpleNamespace(best_compact='未知の通知書',compact='未知の通知書',
            best_text='Example B 様\n未知の通知書',text='Example B 様\n未知の通知書')
        data=SimpleNamespace(pages=[page],best_compact=page.best_compact,compact=page.compact)
        with patch('pdf_batch.load_pdf',return_value=data):
            with self.assertRaises(ArchivedUnparsedDocument) as raised:classify('unused.pdf')
        self.assertEqual(raised.exception.owner,'Example B')

    def test_resolved_tax_charge_remains_non_income_with_no_guessed_event(self):
        path=self.folder/'tax.pdf';japanese_pdf(path,'譲渡益税徴収・還付のお知らせ')
        with patch('pdf_batch.parse_notice',side_effect=LedgerError('解析済み：税金の徴収通知。還付入金ではありません。')):
            with self.assertRaises(LedgerError) as raised:classify(path)
        self.assertNotIsInstance(raised.exception,ArchivedUnparsedDocument)
        self.assertTrue(str(raised.exception).startswith('解析済み：'))


class RetainedPDFWorkflow(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        store,_=seed_fixture(self.base);init_observations(store)
        self.before=fingerprint(store.db);store.close()
        self.folder=self.base/'data/電子ポストPDF/A';self.folder.mkdir(parents=True)

    def table_rows(self):
        store=Store(self.base/'data')
        try:return {table:[tuple(row) for row in store.db.execute('SELECT * FROM '+table)]
            for table in ('accounts','trades','imports','activities','asset_snapshots','account_observations')}
        finally:store.close()

    def new_trade(self):
        path=self.folder/'z_trade.pdf';japanese_pdf(path,'株式取引報告書')
        raw=path.read_bytes()
        trade=dict(trade_date='2026-11-23',settlement_date='2026-11-27',name='New anonymous security',
            code='1234',market='東証',side='買',custody='特定',tax_class='',quantity='2',
            price='100',fees='0',tax='0',settlement_amount='200')
        return path,Snapshot(raw,path.name,hashlib.sha256(raw).hexdigest(),
            '2026-11-23','2026-11-23',[trade],kind='pdf',owner='Example A',document_key='new-anonymous-report')

    def test_unknown_and_specific_notice_do_not_block_valid_trade_or_assets(self):
        unknown=self.folder/'a_unknown.pdf';japanese_pdf(unknown,'未知の通知書')
        specific=self.folder/'b_basis.pdf';japanese_pdf(specific,'特定累積投資勘定基準額等通知書')
        path,doc=self.new_trade();originals={p:p.read_bytes() for p in self.folder.glob('*.pdf')}
        before=self.table_rows();reviews=[]
        def collect(account,stage):
            save_observation(stage,account,'口座サマリー','',{'cash_total':123})
            return dict(complete=True,assets_observed=False,assets_missing=['domestic'])
        def review(summary,proposal,prepared):
            reviews.append(proposal)
            self.assertEqual(summary['excluded_pdf_files'],2)
            self.assertEqual(summary['new_trades'],1)
            self.assertIn('保管のみ',{row['kind'] for row in proposal})
            self.assertTrue(any(w.startswith('PDF_NOT_PARSED:') for w in summary['warnings']))
            return True
        with patch('pdf_batch.parse_report',return_value=doc):
            result=Workflow(self.base).run('2026-11',accounts=('A',),collect=collect,review=review)
        after=self.table_rows()
        self.assertEqual(len(after['trades']),len(before['trades'])+1)
        self.assertEqual(len(after['account_observations']),1)
        self.assertEqual(after['accounts'],before['accounts'])
        self.assertEqual(after['activities'],before['activities'])
        self.assertEqual(after['asset_snapshots'],before['asset_snapshots'])
        self.assertTrue(result['output'].is_file())
        self.assertEqual({p:p.read_bytes() for p in originals},originals)
        state=json.loads((result['job']/'state.json').read_text())
        self.assertEqual(len(state['retained_pdfs']),2)
        self.assertEqual(state['retained_pdfs'][0]['sha256'],hashlib.sha256(originals[unknown]).hexdigest())
        with patch('pdf_batch.parse_report',side_effect=AssertionError('Already imported trade must not be parsed again')):
            repeated=Workflow(self.base).run('2026-11',accounts=('A',),review=lambda *args:True)
        self.assertEqual(repeated['summary']['new_trades'],0)
        self.assertEqual(repeated['summary']['known_pdf_files'],1)
        self.assertEqual(len(self.table_rows()['trades']),len(after['trades']))

    def test_retained_unknown_only_does_not_create_financial_or_import_rows(self):
        path=self.folder/'unknown.pdf';japanese_pdf(path,'未知のお知らせ')
        before=self.table_rows()
        result=Workflow(self.base).run('2026-11',accounts=('A',),review=lambda *args:True)
        self.assertEqual(self.table_rows(),before)
        self.assertEqual(result['summary']['new_trades'],0)
        self.assertEqual(result['summary']['new_cash_or_splits'],0)
        self.assertEqual(result['summary']['excluded_pdf_files'],1)

    def test_parser_failure_is_reviewed_unparsed_while_other_valid_trade_applies(self):
        bad=self.folder/'a_unknown_layout.pdf';japanese_pdf(bad,'投資信託取引報告書')
        path,doc=self.new_trade()
        def parse(value):
            if Path(value)==bad:raise LedgerError('明細の株数と金額を確認できません')
            return doc
        proposals=[]
        with patch('pdf_batch.parse_report',side_effect=parse):
            result=Workflow(self.base).run('2026-11',accounts=('A',),
                review=lambda summary,proposal,prepared:proposals.extend(proposal) or True)
        self.assertEqual(result['summary']['new_trades'],1)
        self.assertEqual(result['summary']['excluded_pdf_files'],1)
        self.assertTrue(any(row['name']==bad.name and row['kind']=='保管のみ' for row in proposals))

    def test_known_wrong_owner_still_blocks_the_complete_review(self):
        path,doc=self.new_trade();doc.owner='Different owner'
        review=Mock(return_value=True);before=self.table_rows()
        with patch('pdf_batch.parse_report',return_value=doc),self.assertRaisesRegex(LedgerError,'名義'):
            Workflow(self.base).run('2026-11',accounts=('A',),review=review)
        self.assertEqual(self.table_rows(),before);review.assert_not_called()

    def test_retained_document_with_explicit_wrong_owner_still_blocks(self):
        path=self.folder/'unknown.pdf';japanese_pdf(path,'未知のお知らせ')
        error=ArchivedUnparsedDocument('未確認の形式');error.owner='Different owner'
        review=Mock(return_value=True);before=self.table_rows()
        with patch('update_workflow.classify',side_effect=error),self.assertRaisesRegex(LedgerError,'名義'):
            Workflow(self.base).run('2026-11',accounts=('A',),review=review)
        self.assertEqual(self.table_rows(),before);review.assert_not_called()

    def test_ledger_conflict_remains_fatal_outside_tolerant_parser_boundary(self):
        path,doc=self.new_trade();review=Mock(return_value=True);before=self.table_rows()
        with patch('pdf_batch.parse_report',return_value=doc), \
             patch('update_workflow._apply',side_effect=LedgerError('登録済み取引と一致しません。')), \
             self.assertRaises(LedgerError):
            Workflow(self.base).run('2026-11',accounts=('A',),review=review)
        self.assertEqual(self.table_rows(),before);review.assert_not_called()

    def test_retained_source_change_after_review_blocks_commit(self):
        path=self.folder/'unknown.pdf';japanese_pdf(path,'未知のお知らせ')
        before=self.table_rows()
        def review(*args):path.write_bytes(b'changed after inspection');return True
        with self.assertRaises(UpdateError) as raised:
            Workflow(self.base).run('2026-11',accounts=('A',),review=review)
        self.assertEqual(raised.exception.code,'SOURCE_CHANGED')
        self.assertEqual(self.table_rows(),before)

    def test_cancel_preserves_retained_original_and_live_data(self):
        path=self.folder/'unknown.pdf';japanese_pdf(path,'未知のお知らせ')
        raw=path.read_bytes();before=self.table_rows()
        with self.assertRaises(Cancelled):
            Workflow(self.base).run('2026-11',accounts=('A',),review=lambda *args:False)
        self.assertEqual(self.table_rows(),before);self.assertEqual(path.read_bytes(),raw)

    def test_corrupt_pdf_is_still_a_critical_error(self):
        path=self.folder/'broken.pdf';path.write_bytes(b'not a PDF')
        before=self.table_rows();review=Mock(return_value=True)
        with self.assertRaises(UpdateError) as raised:
            Workflow(self.base).run('2026-11',accounts=('A',),review=review)
        self.assertEqual(raised.exception.code,'UNSUPPORTED_PDF')
        self.assertEqual(self.table_rows(),before);review.assert_not_called()


if __name__=='__main__':unittest.main()
