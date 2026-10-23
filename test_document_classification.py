"""Document titles/identifiers take precedence over disclosure text."""
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ledger import LedgerError
from pdf_batch import classify,KnownDocumentExclusion,ArchivedUnparsedDocument


def pdf(first, later=''):
    page=SimpleNamespace(best_compact=first,compact=first)
    return SimpleNamespace(pages=[page],best_compact=first+later,compact=first+later)


def japanese_pdf(path,text):
    """Generate a real PDF text layer without bundling a font or user data."""
    from pypdf import PdfWriter
    from pypdf.generic import (ArrayObject,DecodedStreamObject,DictionaryObject,
                               NameObject,NumberObject,TextStringObject)
    name=lambda value:NameObject('/'+value)
    writer=PdfWriter();page=writer.add_blank_page(width=600,height=800)
    descriptor=DictionaryObject({name('Type'):name('FontDescriptor'),
        name('FontName'):name('HeiseiMin-W3'),name('Flags'):NumberObject(4),
        name('FontBBox'):ArrayObject([NumberObject(n) for n in (0,-200,1000,900)]),
        name('ItalicAngle'):NumberObject(0),name('Ascent'):NumberObject(900),
        name('Descent'):NumberObject(-200),name('CapHeight'):NumberObject(700),
        name('StemV'):NumberObject(80)})
    cid=DictionaryObject({name('Type'):name('Font'),name('Subtype'):name('CIDFontType0'),
        name('BaseFont'):name('HeiseiMin-W3'),name('CIDSystemInfo'):DictionaryObject({
            name('Registry'):TextStringObject('Adobe'),name('Ordering'):TextStringObject('Japan1'),
            name('Supplement'):NumberObject(6)}),name('FontDescriptor'):writer._add_object(descriptor),
        name('DW'):NumberObject(1000)})
    cmap=DecodedStreamObject()
    pairs='\n'.join(f'<{index:04X}> <{ord(char):04X}>' for index,char in enumerate(text,1))
    cmap.set_data(('/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n'
        '/CMapType 2 def\n/CMapName /Fixture def\n1 begincodespacerange\n'
        '<0000> <FFFF>\nendcodespacerange\n'+str(len(text))+' beginbfchar\n'+pairs+
        '\nendbfchar\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n').encode())
    font=DictionaryObject({name('Type'):name('Font'),name('Subtype'):name('Type0'),
        name('BaseFont'):name('HeiseiMin-W3'),name('Encoding'):name('Identity-H'),
        name('DescendantFonts'):ArrayObject([writer._add_object(cid)]),
        name('ToUnicode'):writer._add_object(cmap)})
    page[name('Resources')]=DictionaryObject({name('Font'):DictionaryObject({name('F1'):writer._add_object(font)})})
    stream=DecodedStreamObject()
    stream.set_data(('BT /F1 12 Tf 30 740 Td <'+''.join(f'{n:04X}' for n in range(1,len(text)+1))+'> Tj ET').encode())
    page[name('Contents')]=writer._add_object(stream)
    writer.write(path)


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

    def test_unknown_title_is_retained_unparsed_instead_of_becoming_a_known_type(self):
        with patch('pdf_batch.load_pdf',return_value=pdf('未確認の外国株式等のお知らせ')):
            with self.assertRaises(LedgerError) as error:
                classify('unused.pdf')
            self.assertFalse(str(error.exception).startswith('対象外：'))
            self.assertIsInstance(error.exception,ArchivedUnparsedDocument)
            self.assertEqual(error.exception.code,'UNPARSED_PDF')

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

    def test_nisa_transfer_notice_reports_unapplied_account_and_cost_effects(self):
        with patch('pdf_batch.load_pdf',return_value=pdf('非課税口座内上場株式等払出通知書')), \
                patch('pdf_batch.parse_report') as trades,patch('pdf_batch.parse_notice') as actions:
            with self.assertRaises(KnownDocumentExclusion) as error:
                classify('unrelated-filename.pdf')
        notice=error.exception
        self.assertEqual(notice.code,'NISA_TRANSFER_OUT')
        self.assertEqual(notice.document_title,'非課税口座内上場株式等払出通知書')
        self.assertTrue(notice.needs_review)
        self.assertIn('NISAからの払出し・口座区分の移動は未反映',notice.reason)
        self.assertIn('売却取引・税額・取得単価を推定せず',notice.reason)
        self.assertIn('PDF原本を保持',notice.reason)
        trades.assert_not_called();actions.assert_not_called()

    def test_real_generated_nisa_notice_normalizes_spaced_title_and_retains_original(self):
        from pdf_common import load_pdf
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'arbitrary-name.pdf'
            japanese_pdf(path,'非 課 税 口 座 内 上 場 株 式 等 払 出 通 知 書')
            before=path.read_bytes()
            self.assertEqual(load_pdf(path).pages[0].best_compact,'非課税口座内上場株式等払出通知書')
            with self.assertRaises(KnownDocumentExclusion) as error:
                classify(path)
            self.assertEqual(error.exception.code,'NISA_TRANSFER_OUT')
            self.assertEqual(path.read_bytes(),before)

    def test_nisa_filename_cannot_skip_unknown_or_corrupt_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'2023_12_11_非課税口座内上場株式等払出通知書_hash.pdf'
            japanese_pdf(path,'未確認の通知書')
            with self.assertRaises(LedgerError) as error:
                classify(path)
            self.assertNotIsInstance(error.exception,KnownDocumentExclusion)
            self.assertFalse(str(error.exception).startswith('対象外：'))
            path.write_bytes(b'broken-pdf')
            with self.assertRaisesRegex(LedgerError,'PDFを読み取れません') as error:
                classify(path)
            self.assertNotIsInstance(error.exception,KnownDocumentExclusion)
            self.assertEqual(path.read_bytes(),b'broken-pdf')

    def test_nisa_reference_does_not_shadow_identified_balance_report(self):
        for first,later in (('STWHZB非課税口座内上場株式等払出通知書',''),
                            ('取引残高報告書','非課税口座内上場株式等払出通知書')):
            with self.subTest(first=first),patch('pdf_batch.load_pdf',return_value=pdf(first,later)), \
                    patch('pdf_batch.parse_balance',return_value='balance'):
                self.assertEqual(classify('unused.pdf'),('balance','balance'))

    def test_later_page_nisa_title_does_not_make_unknown_first_page_known(self):
        with patch('pdf_batch.load_pdf',return_value=pdf('未確認の通知書','非課税口座内上場株式等払出通知書')):
            with self.assertRaises(LedgerError) as error:
                classify('unused.pdf')
            self.assertNotIsInstance(error.exception,KnownDocumentExclusion)

    def test_generic_nisa_or_notification_words_never_authorize_exclusion(self):
        for title in ('NISAの重要なお知らせ','非課税口座の通知書','非課税口座内上場株式等の取引資料'):
            with self.subTest(title=title),patch('pdf_batch.load_pdf',return_value=pdf(title)):
                with self.assertRaises(LedgerError) as error:
                    classify('unused.pdf')
                self.assertNotIsInstance(error.exception,KnownDocumentExclusion)

    def test_annual_registry_policy_is_distinct_from_unparsed_financial_events(self):
        with patch('pdf_batch.load_pdf',return_value=pdf('STWHGB')):
            with self.assertRaises(KnownDocumentExclusion) as error:
                classify('unused.pdf')
        self.assertEqual(error.exception.code,'ANNUAL_TAX_REPORT')
        self.assertFalse(error.exception.needs_review)
        self.assertNotIn('未解析',str(error.exception))


if __name__=='__main__':
    unittest.main()
