"""Document titles/identifiers take precedence over disclosure text."""
from types import SimpleNamespace
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


if __name__=='__main__':
    unittest.main()
