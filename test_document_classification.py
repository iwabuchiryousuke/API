"""Document titles/identifiers take precedence over disclosure text."""
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ledger import LedgerError
from pdf_batch import classify


def pdf(first, later=''):
    page=SimpleNamespace(best_compact=first,compact=first)
    return SimpleNamespace(pages=[page],best_compact=first+later,compact=first+later)


class DocumentClassificationTests(unittest.TestCase):
    def test_balance_identifiers_override_bond_disclosure(self):
        for marker in ('STWHZB','FR00011M01'):
            with self.subTest(marker=marker), \
                    patch('pdf_batch.load_pdf',return_value=pdf(marker+'お預り残高等の明細',
                        '外国債券の利金お支払、償還金について特定口座年間取引報告書を参照')), \
                    patch('pdf_batch.parse_balance',return_value='balance') as parser:
                self.assertEqual(classify('unused.pdf'),('balance','balance'))
                parser.assert_called_once_with('unused.pdf')

    def test_balance_title_is_not_bond_interest_title(self):
        with patch('pdf_batch.load_pdf',return_value=pdf('取引残高報告書外国債券利金償還金')), \
                patch('pdf_batch.parse_balance',return_value='balance'):
            self.assertEqual(classify('unused.pdf'),('balance','balance'))

    def test_actual_bond_interest_title_stays_explicitly_unsupported(self):
        with patch('pdf_batch.load_pdf',return_value=pdf(
                '外国債券利金・償還金お支払のご案内(兼)支払通知書')):
            with self.assertRaisesRegex(LedgerError,'対象外：外国債券の利金'):
                classify('unused.pdf')

    def test_bond_trade_is_not_mislabeled_interest(self):
        with patch('pdf_batch.load_pdf',return_value=pdf(
                '外国債券取引報告書利金償還お支払')):
            with self.assertRaisesRegex(LedgerError,'対象外：外国債券取引報告書'):
                classify('unused.pdf')

    def test_foreign_share_and_annual_are_not_domestic_trades(self):
        for title in ('外国株式等取引報告書','特定口座年間取引報告書'):
            with self.subTest(title=title), \
                    patch('pdf_batch.load_pdf',return_value=pdf(title)), \
                    patch('pdf_batch.parse_report') as domestic:
                with self.assertRaisesRegex(LedgerError,'対象外：'+title):
                    classify('unused.pdf')
                domestic.assert_not_called()

    def test_foreign_corporate_action_title_is_explicitly_unparsed(self):
        with patch('pdf_batch.load_pdf',return_value=pdf(
                '外国株式等株式分割・権利売却等のご案内')), \
                patch('pdf_batch.parse_notice') as notice, \
                patch('pdf_batch.parse_report') as domestic:
            # The name is deliberately unrelated: recognition is from content.
            with self.assertRaisesRegex(LedgerError,
                    r'^対象外：外国株式等株式分割・権利売却等のご案内（未解析）') as error:
                classify('arbitrary-name.pdf')
            self.assertIn('株数・売却代金には反映しません',str(error.exception))
            self.assertIn('PDF原本を保持',str(error.exception))
            notice.assert_not_called()
            domestic.assert_not_called()

    def test_foreign_corporate_action_in_disclosure_is_not_its_title(self):
        with patch('pdf_batch.load_pdf',return_value=pdf('取引残高報告書',
                '外国株式等株式分割・権利売却等のご案内')), \
                patch('pdf_batch.parse_balance',return_value='balance'):
            self.assertEqual(classify('unused.pdf'),('balance','balance'))

    def test_balance_identifier_takes_precedence_over_foreign_action_reference(self):
        with patch('pdf_batch.load_pdf',return_value=pdf(
                'STWHZB外国株式等株式分割・権利売却等のご案内')), \
                patch('pdf_batch.parse_balance',return_value='balance'):
            self.assertEqual(classify('unused.pdf'),('balance','balance'))

    def test_unknown_document_title_remains_a_blocker(self):
        with patch('pdf_batch.load_pdf',return_value=pdf('未確認の外国株式等のお知らせ')):
            with self.assertRaises(LedgerError) as error:
                classify('unused.pdf')
            self.assertFalse(str(error.exception).startswith('対象外：'))

    def test_filename_alone_does_not_exclude_unknown_or_corrupt_pdf(self):
        from pypdf import PdfWriter
        from pypdf.generic import (DictionaryObject,NameObject,DecodedStreamObject)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'2023_12_07_外国株式等株式分割_権利売却等のご案内.pdf'
            writer=PdfWriter()
            page=writer.add_blank_page(width=600,height=800)
            font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),
                NameObject('/Subtype'):NameObject('/Type1'),
                NameObject('/BaseFont'):NameObject('/Helvetica')})
            page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):
                DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
            contents=DecodedStreamObject()
            contents.set_data(b'BT /F1 12 Tf 30 700 Td (Unknown report) Tj ET')
            page[NameObject('/Contents')]=writer._add_object(contents)
            writer.write(path)
            before=path.read_bytes()
            with self.assertRaises(LedgerError) as error:
                classify(path)
            self.assertFalse(str(error.exception).startswith('対象外：'))
            self.assertEqual(path.read_bytes(),before)
            path.write_bytes(b'not a PDF')
            with self.assertRaisesRegex(LedgerError,'PDFを読み取れません') as error:
                classify(path)
            self.assertFalse(str(error.exception).startswith('対象外：'))
            self.assertEqual(path.read_bytes(),b'not a PDF')


if __name__=='__main__':
    unittest.main()
