"""Exact uploaded-format contracts, using wholly synthetic financial values.

The expected style fingerprint and geometry were measured independently from
the attachment. Tests do not require the private workbook or openpyxl at runtime.
"""
from copy import deepcopy
from datetime import date
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from excel_template_v361 import LAYOUT,apply_template_layout,write_template_model
from standalone_excel import write_model

NS='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
X={'x':NS}


def cell(row,column,value=None,kind='text',**extra):
    return dict(r=row,c=column,v=value,k=kind,fill=None,**extra)


def synthetic_model():
    trade=dict(name='取引',cells=[],widths={},freeze='C2',filter='A1:M4',merges=['A100:B100'])
    for row,fill,side in ((2,'DDEBF7','B'),(3,'FCE4D6','S'),(4,None,'B')):
        values=['2026-11-07','Synthetic A','Example Security',9900,'N-成',side,2,124.75,249.5,0]
        for column,value in enumerate(values,1):
            current=cell(row,column,value,'date' if column==1 else 'number' if isinstance(value,(int,float)) else 'text')
            current['fill']=fill;trade['cells'].append(current)
        for column,formula in ((11,'2'),(12,'125'),(13,'0')):
            trade['cells'].append(cell(row,column,formula,'formula_number'))
    trade['cells'].append(cell(1,15,'2026-11-07','date'))
    trade['cells'].append(cell(2,15,'Synthetic A'))
    for column,value in ((16,1000),(17,2000),(18,0),(19,123.45)):
        trade['cells'].append(cell(2,column,value,'number'))
    dividend=dict(name='配当',cells=[],widths={},freeze='C2',filter=None,merges=[],
                  blocks=dict(summary_last_row=11,cumulative_header_row=13,
                              cumulative_total_row=17,cumulative_last_row=18,
                              calendar_label_row=20,calendar_last_row=24),
                  conditional_formats=[dict(range='K21:N24',dxf=1,
                                            formula='AND(ISNUMBER($K21),$K21>=TODAY(),$K21<=EDATE(TODAY(),1))')])
    detail=['2026-11-01','Synthetic A','Example Security',9900,'N-成','配当',2,50,100]
    for column,value in enumerate(detail,1):
        dividend['cells'].append(cell(2,column,value,'date' if column==1 else 'number' if isinstance(value,(int,float)) else 'text'))
    for row in range(2,11):
        for column,value in ((11,9900+row),(12,'Example '+str(row)),(13,'3/27・9/28'),(14,'無配'),
                             (15,'3/4' if row==2 else '未定'),(16,2),(17,50),(18,.02),
                             (19,100),(20,50),(21,150),(22,100),(23,2500),(24,2600)):
            dividend['cells'].append(cell(row,column,value,'number' if isinstance(value,(int,float)) else 'text'))
    dividend['cells'].append(cell(11,11,'合計'))
    for column in (19,20,21,22):dividend['cells'].append(cell(11,column,'SUM(S2:S10)','formula_number'))
    for column,label in ((11,'年'),(12,'配当'),(13,'税還'),(14,'利息')):
        dividend['cells'].append(cell(13,column,label))
    for row,year in enumerate((2024,2025,2026),14):
        dividend['cells'].append(cell(row,11,year,'number'))
        for column in (12,13,14):dividend['cells'].append(cell(row,column,100,'number'))
    dividend['cells'].append(cell(17,11,'累計'))
    for column in (12,13,14):dividend['cells'].append(cell(17,column,'SUM(L14:L16)','formula_number'))
    dividend['cells'].extend([cell(18,11,'総計'),cell(18,12,'SUM(L17:N17)','formula_number'),cell(20,11,2026,'number')])
    for row in range(21,25):
        dividend['cells'].extend([cell(row,11,f'2026-11-{row-13:02d}','date',format='m/d'),
                                  cell(row,12,'Example Security'),
                                  cell(row,13,'配当金支払日' if row>=23 else '権利付き最終日')])
    dividend['cells'].extend([cell(23,14,100,'number',format='#,##0"（実績）"'),
                              cell(24,14,200,'number',format='#,##0"（予想）"')])
    apply_template_layout([trade,dividend])
    return dict(as_of='2026-11-30',layout=LAYOUT,sheets=[trade,dividend])


def structural_value(node):
    return [node.tag,sorted(node.attrib.items()),node.text or '',[structural_value(child) for child in node]]


class ExactUploadedTemplate(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.directory=Path(temporary.name);self.model=synthetic_model()
        self.path=write_model(self.model,self.directory)
        self.archive=ZipFile(self.path);self.addCleanup(self.archive.close)
        self.styles=ET.fromstring(self.archive.read('xl/styles.xml'))
        self.trade=ET.fromstring(self.archive.read('xl/worksheets/sheet1.xml'))
        self.dividend=ET.fromstring(self.archive.read('xl/worksheets/sheet2.xml'))

    def style(self,sheet,reference):
        current=sheet.find('.//x:c[@r="'+reference+'"]',X)
        return int(current.get('s','0'))

    def test_original_font_fill_alignment_and_number_formats_are_unchanged(self):
        tags=['numFmts','fonts','fills','borders','cellStyleXfs','cellXfs','cellStyles']
        parts=[]
        for tag in tags:
            node=deepcopy(self.styles.find('x:'+tag,X))
            count={'numFmts':6,'cellXfs':30}.get(tag)
            if count:
                for child in list(node)[count:]:node.remove(child)
                node.set('count',str(count))
            parts.append(structural_value(node))
        measured=hashlib.sha256(json.dumps(parts,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        self.assertEqual(measured,'aa81028c0b5996a31bf950d6bb9684d189dd49a31d626bbcea12a3e28f65b1bc')

    def test_theme_matches_uploaded_workbook_exactly(self):
        self.assertEqual(hashlib.sha256(self.archive.read('xl/theme/theme1.xml')).hexdigest(),
                         '6467cd8adf5c5cdb4f1ed32e23259c168ea23e45b75e2e0f57635b80952d664c')

    def test_column_widths_group_spans_and_default_styles_match(self):
        expected_trade=[(1,1,'13','1'),(2,2,'5.625','1'),(3,3,'15.625','1'),(4,5,'7.625','1'),
                        (6,6,'3.125','1'),(7,7,'7.75','1'),(8,8,'9.625','1'),(9,9,'15.5','1'),
                        (10,11,'8.625','1'),(12,12,'9.5','2'),(13,13,'11.75','15'),(14,14,'9','1'),
                        (15,15,'10.5','1'),(16,17,'10.625','1'),(18,18,'13.625','1'),
                        (19,19,'10.625','1'),(20,16384,'9','1')]
        expected_dividend=[(1,1,'13','8'),(2,2,'5.625','8'),(3,3,'15.625','8'),(4,5,'7.625','8'),
                           (6,6,'6.25','8'),(7,7,'7.75','1'),(8,8,'8.75','1'),(9,9,'12.75','1'),
                           (10,10,'9','1'),(11,11,'7.75','8'),(12,12,'15.75','1'),(13,13,'14.75','1'),
                           (14,14,'14.625','1'),(15,15,'13.75','1'),(16,17,'6.625','1'),
                           (18,18,'8.625','1'),(19,21,'10.625','1'),(22,22,'13.625','1'),(23,16384,'9','1')]
        for sheet,expected in ((self.trade,expected_trade),(self.dividend,expected_dividend)):
            self.assertEqual([(int(node.get('min')),int(node.get('max')),node.get('width'),node.get('style'))
                              for node in sheet.find('x:cols',X)],expected)
        self.assertEqual(self.trade.find('x:cols/x:col[@min="15"]',X).get('bestFit'),'1')

    def test_rows_keep_exact_heights_including_the_empty_grid(self):
        for sheet,last,default in ((self.trade,83,'15.75'),(self.dividend,82,'14.25')):
            self.assertEqual(sheet.find('x:sheetFormatPr',X).get('defaultRowHeight'),default)
            for number in range(1,last+1):
                node=sheet.find(f'x:sheetData/x:row[@r="{number}"]',X)
                self.assertEqual(float(node.get('ht')),20.1)
                self.assertEqual(node.get('customHeight'),'1')
        for number in range(84,88):
            self.assertIsNone(self.trade.find(f'x:sheetData/x:row[@r="{number}"]',X).get('ht'))
            self.assertEqual(self.style(self.trade,'K'+str(number)),3)

    def test_new_rows_extend_the_same_height_without_resizing_existing_rows(self):
        self.model['sheets'][0]['cells'].append(cell(90,1,'2026-11-01','date'))
        output=write_model(self.model,self.directory)
        with ZipFile(output) as archive:
            sheet=ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        for number in range(84,91):
            current=sheet.find(f'x:sheetData/x:row[@r="{number}"]',X)
            if number in (84,85,86,87):continue
            self.assertEqual(float(current.get('ht')),20.1)

    def test_no_columns_or_rows_are_frozen_or_filtered_and_zoom_matches(self):
        for sheet,zoom in ((self.trade,'73'),(self.dividend,'86')):
            self.assertIsNone(sheet.find('.//x:pane',X))
            self.assertIsNone(sheet.find('x:autoFilter',X));self.assertIsNone(sheet.find('x:mergeCells',X))
            self.assertEqual(sheet.find('x:sheetViews/x:sheetView',X).get('zoomScale'),zoom)
            self.assertEqual(sheet.find('x:pageMargins',X).attrib,
                             dict(left='0.3',right='0.3',top='0.5',bottom='0.5',header='0.2',footer='0.2'))

    def test_trade_headers_and_buy_sell_split_styles_are_exact(self):
        expected={'H1':'単価','I1':'約定・受取額','J1':'手数料・税','K1':'保有数','L1':'平均単価'}
        for reference,value in expected.items():
            self.assertEqual(self.trade.find('.//x:c[@r="'+reference+'"]/x:is/x:t',X).text,value)
        for row,styles in ((2,[7,5,5,6,5,5,4,4,4,4,2,2,15]),
                           (3,[14,12,12,13,12,12,11,11,11,11,2,2,15]),
                           (4,[10,8,8,9,8,8,3,3,3,3,2,2,15])):
            self.assertEqual([self.style(self.trade,chr(64+column)+str(row)) for column in range(1,14)],styles)
        self.assertEqual([self.style(self.trade,reference) for reference in ('O1','O2','P2','Q2','R2','S2')],
                         [10,1,3,3,3,3])

    def test_stored_values_and_running_formulas_survive_integer_display_formats(self):
        self.assertEqual(self.trade.find('.//x:c[@r="H2"]/x:v',X).text,'124.75')
        self.assertEqual(self.trade.find('.//x:c[@r="S2"]/x:v',X).text,'123.45')
        self.assertEqual(self.trade.find('.//x:c[@r="K2"]/x:f',X).text,'2')
        self.assertEqual(self.trade.find('.//x:c[@r="O1"]/x:v',X).text,str((date(2026,11,7)-date(1899,12,30)).days))

    def test_dividend_total_annual_and_cumulative_fills_match(self):
        for reference,expected in {'K11':8,'S11':22,'T11':25,'U11':25,'V11':23,'W11':3,'X11':3,
                                   'K13':8,'N13':1,'K14':18,'L14':17,'M14':17,'N14':3,
                                   'L16':21,'L17':24,'M17':17,'N17':3,'K18':8,'L18':17,
                                   'O2':20,'O3':1}.items():
            self.assertEqual(self.style(self.dividend,reference),expected,reference)
        for row in (12,19):
            right_cells=[node for node in self.dividend.findall(f'x:sheetData/x:row[@r="{row}"]/x:c',X)
                         if node.get('r').rstrip('0123456789') in tuple('KLMNOPQRSTUVWX')]
            self.assertTrue(all(node.find('x:v',X) is None and node.find('x:is',X) is None and node.find('x:f',X) is None
                                for node in right_cells))

    def test_calendar_only_overrides_explicit_date_and_forecast_formats(self):
        xfs=list(self.styles.find('x:cellXfs',X))
        formats={node.get('numFmtId'):node.get('formatCode') for node in self.styles.find('x:numFmts',X)}
        for reference,base,expected in (('K21',10,'m/d'),('N24',16,'#,##0"（予想）"')):
            actual=xfs[self.style(self.dividend,reference)];original=xfs[base]
            self.assertEqual(formats[actual.get('numFmtId')],expected)
            for key in ('fontId','fillId','borderId','xfId'):self.assertEqual(actual.get(key),original.get(key))
            self.assertEqual(structural_value(actual.find('x:alignment',X)),
                             structural_value(original.find('x:alignment',X)))
        self.assertEqual(self.style(self.dividend,'N23'),16)

    def test_all_calendar_events_within_one_month_get_the_template_red(self):
        rule=self.dividend.find('x:conditionalFormatting/x:cfRule',X)
        self.assertEqual(self.dividend.find('x:conditionalFormatting',X).get('sqref'),'K21:N24')
        self.assertEqual(rule.find('x:formula',X).text,
                         'AND(ISNUMBER($K21),$K21>=TODAY(),$K21<=EDATE(TODAY(),1))')
        differential=list(self.styles.find('x:dxfs',X))[int(rule.get('dxfId'))]
        self.assertEqual(differential.find('x:fill/x:patternFill/x:fgColor',X).get('rgb'),'FFFF7C80')
        self.assertEqual(differential.find('x:fill/x:patternFill/x:bgColor',X).get('rgb'),'FFFF7C80')

    def test_only_public_appearance_is_carried_and_text_never_becomes_a_formula(self):
        self.assertFalse(any('externalLink' in name or 'sharedStrings' in name or 'calcChain' in name or 'metadata' in name
                             for name in self.archive.namelist()))
        self.assertFalse(any('[1]' in self.archive.read(name).decode('utf-8') for name in self.archive.namelist()))
        self.model['sheets'][0]['cells'].append(cell(5,3,'=HYPERLINK("https://example.invalid")'))
        output=write_model(self.model,self.directory)
        with ZipFile(output) as archive:
            current=ET.fromstring(archive.read('xl/worksheets/sheet1.xml')).find('.//x:c[@r="C5"]',X)
        self.assertEqual(current.get('t'),'inlineStr');self.assertIsNone(current.find('x:f',X))

    def test_invalid_value_cannot_replace_existing_output(self):
        target=self.directory/'existing.xlsx';target.write_bytes(b'unchanged existing output')
        self.model['sheets'][0]['cells'].append(cell(5,1,float('nan'),'number'))
        with self.assertRaises(ValueError):write_template_model(self.model,target)
        self.assertEqual(target.read_bytes(),b'unchanged existing output')
        self.assertFalse(target.with_suffix('.tmp').exists())


if __name__=='__main__':unittest.main()
