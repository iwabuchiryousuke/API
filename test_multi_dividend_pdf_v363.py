"""Real, anonymous multi-security PDF text layers; no uploaded data bundled."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from corporate_actions import parse_notice
from ledger import LedgerError
from pdf_batch import ArchivedUnparsedDocument, classify


def notice_lines(*, second_code='9911', second_name='長い架空の昇降機関連銘柄のテスト名称',
                 second_date='2025年 6月25日', second_dps='3.0000000',
                 second_quantity='10', second_amounts='30 0 0 0 30',
                 second_custody='NISA成長投資枠', extra_owner=False):
    lines=['株式等配当金のお知らせ','作成日:2025年 6月24日',
           '特定口座契約区分:源泉徴収あり','架空 試験 様',
           '短い架空銘柄','2025年 6月25日 5.0000000 7','(9900 )',
           '35 0 0 0 35','NISA成長投資枠 2025年 3月31日']
    if extra_owner:lines.append('別の 架空名義 様')
    try:total=35+int(second_amounts.split()[-1].replace(',',''))
    except (IndexError,ValueError):total=65
    lines.extend([second_name,f'{second_date} {second_dps} {second_quantity}',
                  f'({second_code} )',second_amounts,
                  second_custody+' 2025年 3月31日',f'受取額合計 {total}'])
    return lines


def write_notice_pdf(path,lines):
    """Generate small landscape text rows with an explicit Unicode font map."""
    from pypdf import PdfReader,PdfWriter
    from pypdf.generic import DecodedStreamObject,NameObject,NumberObject,RectangleObject
    from test_document_classification import japanese_pdf
    japanese_pdf(path,''.join(lines))
    reader=PdfReader(path);writer=PdfWriter();writer.clone_document_from_reader(reader)
    page=writer.pages[0];page.mediabox=RectangleObject((0,0,597.6,324))
    commands=[];offset=1
    for index,line in enumerate(lines):
        encoded=''.join(f'{position:04X}' for position in range(offset,offset+len(line)))
        commands.append(f'BT /F1 7 Tf 24 {300-index*13} Td <{encoded}> Tj ET')
        offset+=len(line)
    stream=DecodedStreamObject();stream.set_data('\n'.join(commands).encode('ascii'))
    page[NameObject('/Contents')]=writer._add_object(stream)
    target=Path(path).with_suffix('.new.pdf');writer.write(target);target.replace(path)
    return Path(path)


class MultiDividendPDF(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'anonymous.pdf'

    def document(self,**options):
        write_notice_pdf(self.path,notice_lines(**options))
        return parse_notice(self.path)

    def test_every_security_is_read_and_first_payload_remains_compatible(self):
        document=self.document()
        self.assertEqual(len(document.items),2)
        self.assertEqual(document.item,document.items[0])
        self.assertEqual([item['code'] for item in document.items],['9900','9911'])
        self.assertEqual([item['date'] for item in document.items],['2025-06-25']*2)
        self.assertEqual([item['net'] for item in document.items],['35','30'])
        self.assertEqual(document.items[1]['quantity'],'10')
        self.assertEqual(document.items[1]['per_share'],'3')
        self.assertEqual(document.items[1]['name'],'長い架空の昇降機関連銘柄のテスト名称')
        self.assertEqual(document.digest,hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(classify(self.path)[1].items,document.items)

    def test_second_taxed_security_retains_its_own_money_and_custody(self):
        document=self.document(second_amounts='30 4 1 0 25',second_custody='特定対象')
        first,second=document.items
        self.assertEqual((first['net'],first['custody']),('35','NISA'))
        self.assertEqual((second['gross'],second['tax'],second['net'],second['custody']),('30','5','25',''))

    def test_later_tax_or_quantity_mismatch_rejects_the_whole_notice(self):
        for options in ({'second_amounts':'30 0 0 0 29'},
                        {'second_quantity':'9'},
                        {'second_amounts':'30 0 0 30'}):
            with self.subTest(options=options),self.assertRaises(LedgerError):self.document(**options)
            with self.assertRaises(ArchivedUnparsedDocument):classify(self.path)

    def test_later_unreadable_number_or_code_cannot_be_silently_ignored(self):
        for options in ({'second_dps':'unreadable'}, {'second_code':'?'},
                        {'second_date':'2025年 6月99日'}):
            with self.subTest(options=options),self.assertRaises(LedgerError):self.document(**options)

    def test_later_block_with_both_date_number_and_code_unreadable_is_not_skipped(self):
        for options in ({'second_dps':'unreadable','second_code':'?'},
                        {'second_dps':'unreadable','second_code':'?','second_amounts':'unreadable'},
                        {'second_dps':'unreadable','second_code':'?','second_amounts':'unreadable',
                         'second_custody':'unreadable'}):
            with self.subTest(options=options),self.assertRaises(LedgerError):self.document(**options)
            with self.assertRaises(ArchivedUnparsedDocument):classify(self.path)

    def test_labeled_and_unambiguous_unlabeled_totals_guard_missing_blocks(self):
        for total in ('受取額合計 64','64'):
            lines=notice_lines();lines[-1]=total;write_notice_pdf(self.path,lines)
            with self.subTest(total=total),self.assertRaisesRegex(LedgerError,'合計'):parse_notice(self.path)
        lines=notice_lines();lines[-1]='65';write_notice_pdf(self.path,lines)
        self.assertEqual(len(parse_notice(self.path).items),2)

    def test_indistinguishable_duplicate_rows_require_review(self):
        with self.assertRaisesRegex(LedgerError,'同一明細'):
            self.document(second_code='9900',second_dps='5.0000000',second_quantity='7',
                          second_amounts='35 0 0 0 35')

    def test_distinct_payments_for_same_code_are_not_lost_by_the_parser(self):
        document=self.document(second_code='9900')
        self.assertEqual([item['code'] for item in document.items],['9900','9900'])
        self.assertEqual([item['net'] for item in document.items],['35','30'])

    def test_multiple_owners_are_not_assigned_to_the_first_account(self):
        with self.assertRaisesRegex(LedgerError,'複数の名義'):self.document(extra_owner=True)

    def test_single_security_notice_still_has_one_unmodified_event(self):
        write_notice_pdf(self.path,notice_lines()[:9])
        document=parse_notice(self.path)
        self.assertEqual(len(document.items),1)
        self.assertEqual(document.item,document.items[0])
        self.assertEqual(document.item['code'],'9900')
        self.assertEqual(document.item['net'],'35')

    def test_three_valid_security_blocks_include_the_last_one(self):
        lines=notice_lines()[:-1]
        lines.extend(['第三の架空銘柄','2025年 6月26日 2.0000000 4','(9922 )',
                      '8 0 0 0 8','NISA成長投資枠 2025年 3月31日'])
        write_notice_pdf(self.path,lines)
        document=parse_notice(self.path)
        self.assertEqual([item['code'] for item in document.items],['9900','9911','9922'])
        self.assertEqual(document.items[-1]['date'],'2025-06-26')
        self.assertEqual(document.items[-1]['net'],'8')


if __name__=='__main__':unittest.main()
