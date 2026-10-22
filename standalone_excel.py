"""Desktop OOXML exporter. Standard library only; shared report_model schema.
The review/template workbook is authored separately with artifact-tool.
"""
from datetime import date,datetime
from pathlib import Path
from zipfile import ZipFile,ZIP_DEFLATED
from xml.sax.saxutils import escape,quoteattr
import os
from activity import Activities
from report_model import build,col,NUM

NS='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL='http://schemas.openxmlformats.org/package/2006/relationships'
OFF='http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
def clean(v):return ''.join(c for c in str(v) if ord(c)>=32 or c in '\t\n\r')

def export_trades(store,outdir,month='',activities=None,management=False):
    model=build(store,activities or Activities(store),month,management)
    return write_model(model,outdir,management)

def write_model(model,outdir,management=False):
    outdir=Path(outdir);outdir.mkdir(parents=True,exist_ok=True)
    target=outdir/f'{"管理見本" if management else "売買記録"}_{model["as_of"]}_{datetime.now():%Y%m%d_%H%M%S_%f}.xlsx'
    temporary=target.with_suffix('.tmp')
    styles={};xfs=[];custom_formats={}
    def style(cell):
        typ=cell['k'].removeprefix('formula_');font=1 if typ in ('number','date','percent','code') else 0
        fill={None:0,'DDEBF7':2,'FCE4D6':3,'E7E6E6':4,'FFF2CC':5}.get(cell.get('fill'),0)
        custom=cell.get('format')
        if custom and custom not in custom_formats:custom_formats[custom]=167+len(custom_formats)
        fmt=custom_formats[custom] if custom else {'number':164,'date':165,'percent':166}.get(typ,0)
        border=1 if cell.get('border_top') else 0
        al=cell.get('align',{});h=al.get('h');v=al.get('v') or 'center';wrap=al.get('wrap',True);key=(font,fill,fmt,h,v,wrap,border)
        alignment=f'<alignment vertical="{v}" wrapText="{int(bool(wrap))}"'+(f' horizontal="{h}"' if h else '')+'/>'
        if key not in styles:
            styles[key]=len(xfs);xfs.append(f'<xf fontId="{font}" fillId="{fill}" numFmtId="{fmt}" borderId="{border}" applyBorder="1" xfId="0" applyFont="1" applyFill="1" applyNumberFormat="1">{alignment}</xf>')
        return styles[key]
    style({'k':'text'})
    sheets=[]
    for s in model['sheets']:
        rows={}
        for c in s['cells']:
            r=c['r'];ref=f'{col(c["c"])}{r}';st=style(c);v=c['v'];typ=c['k']
            content=''
            if v is not None and v!='':
                if typ.startswith('formula_'):content=f'<f>{escape(v.lstrip("="))}</f>'
                elif typ=='date':content=f'<v>{(date.fromisoformat(v)-date(1899,12,30)).days}</v>'
                elif isinstance(v,(int,float)):content=f'<v>{v}</v>'
                else:content=f'<is><t xml:space="preserve">{escape(clean(v))}</t></is>'
            attr=' t="inlineStr"' if content.startswith('<is>') else ''
            rows.setdefault(r,[]).append((c['c'],f'<c r="{ref}" s="{st}"{attr}>{content}</c>'))
        data=''.join(f'<row r="{r}" ht="{s.get("header_height",44.1) if r==1 else s.get("row_height",44.1)}" customHeight="1">'+''.join(v for _,v in sorted(cells))+'</row>' for r,cells in sorted(rows.items()))
        cols=''.join(f'<col min="{c}" max="{c}" width="{w}" customWidth="1" hidden="{int(int(c) in s.get("hidden_columns",[]))}"/>' for c,w in s['widths'].items())
        filt=f'<autoFilter ref="{s["filter"]}"/>' if s.get('filter') else ''
        merges=('<mergeCells count="'+str(len(s.get('merges',[])))+'">'+''.join('<mergeCell ref="'+m+'"/>' for m in s.get('merges',[]))+'</mergeCells>') if s.get('merges') else ''
        conditions=''.join(f'<conditionalFormatting sqref="{rule["range"]}"><cfRule type="expression" dxfId="{rule["dxf"]}" priority="{i}" stopIfTrue="1"><formula>{escape(rule["formula"])}</formula></cfRule></conditionalFormatting>' for i,rule in enumerate(s.get('conditional_formats',[]),1))
        drawing='<drawing r:id="chart1"/>' if s.get('chart') else ''
        sheets.append(f'<worksheet xmlns="{NS}" xmlns:r="{OFF[:-1]}"><sheetViews><sheetView workbookViewId="0" showGridLines="1"/></sheetViews><sheetFormatPr defaultRowHeight="24"/><cols>{cols}</cols><sheetData>{data}</sheetData>{filt}{merges}{conditions}<pageMargins left="0.3" right="0.3" top="0.5" bottom="0.5" header="0.2" footer="0.2"/>{drawing}</worksheet>')
    fills=''.join(f'<fill><patternFill patternType="solid"><fgColor rgb="FF{x}"/><bgColor indexed="64"/></patternFill></fill>' for x in ('DDEBF7','FCE4D6','E7E6E6','FFF2CC'))
    custom_xml=''.join(f'<numFmt numFmtId="{ident}" formatCode={quoteattr(fmt)}/>' for fmt,ident in custom_formats.items())
    stylesxml=f'<styleSheet xmlns="{NS}"><numFmts count="{3+len(custom_formats)}"><numFmt numFmtId="164" formatCode={quoteattr(NUM)}/><numFmt numFmtId="165" formatCode="yyyy/mm/dd"/><numFmt numFmtId="166" formatCode="0.00%;[Red]-0.00%;0.00%"/>{custom_xml}</numFmts><fonts count="2"><font><sz val="12"/><name val="ＭＳ Ｐゴシック"/></font><font><sz val="12"/><name val="Times New Roman"/></font></fonts><fills count="6"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>{fills}</fills><borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border><border><left/><right/><top style="thick"><color rgb="FF000000"/></top><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf fontId="0" fillId="0" borderId="0" numFmtId="0"/></cellStyleXfs><cellXfs count="{len(xfs)}">{"".join(xfs)}</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles><dxfs count="2"><dxf><fill><patternFill patternType="solid"><fgColor rgb="FFFCE4D6"/><bgColor indexed="64"/></patternFill></fill></dxf><dxf><fill><patternFill patternType="solid"><fgColor rgb="FFFFF2CC"/><bgColor indexed="64"/></patternFill></fill></dxf></dxfs></styleSheet>'
    try:
        with ZipFile(temporary,'w',ZIP_DEFLATED) as z:
            types=[('/xl/workbook.xml','sheet.main'),('/xl/styles.xml','styles')]+[(f'/xl/worksheets/sheet{i}.xml','worksheet') for i in range(1,len(sheets)+1)]
            overrides=''.join(f'<Override PartName="{p}" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.{t}+xml"/>' for p,t in types)
            if management:overrides+='<Override PartName="/xl/drawings/drawing1.xml" ContentType="application/vnd.openxmlformats-officedocument.drawing+xml"/><Override PartName="/xl/charts/chart1.xml" ContentType="application/vnd.openxmlformats-officedocument.drawingml.chart+xml"/>'
            z.writestr('[Content_Types].xml',f'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>{overrides}</Types>')
            z.writestr('_rels/.rels',f'<Relationships xmlns="{REL}"><Relationship Id="rId1" Type="{OFF}officeDocument" Target="xl/workbook.xml"/></Relationships>')
            wb=''.join(f'<sheet name={quoteattr(s["name"])} sheetId="{i}" state="{"hidden" if s.get("hidden") else "visible"}" r:id="rId{i}"/>' for i,s in enumerate(model['sheets'],1))
            z.writestr('xl/workbook.xml',f'<workbook xmlns="{NS}" xmlns:r="{OFF[:-1]}"><sheets>{wb}</sheets><calcPr calcId="191029" calcMode="auto" fullCalcOnLoad="1" forceFullCalc="1"/></workbook>')
            rels=''.join(f'<Relationship Id="rId{i}" Type="{OFF}worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1,len(sheets)+1))
            z.writestr('xl/_rels/workbook.xml.rels',f'<Relationships xmlns="{REL}">{rels}<Relationship Id="styles" Type="{OFF}styles" Target="styles.xml"/></Relationships>')
            z.writestr('xl/styles.xml',stylesxml)
            for i,s in enumerate(sheets,1):z.writestr(f'xl/worksheets/sheet{i}.xml',s)
            for i,s in enumerate(model['sheets'],1):
                if s.get('chart'):write_chart(z,i,s)
        os.replace(temporary,target)
    finally:
        if temporary.exists():temporary.unlink()
    return target

def write_chart(z,index,sheet):
    chart=sheet['chart'];name=sheet['name'];cns='http://schemas.openxmlformats.org/drawingml/2006/chart';ans='http://schemas.openxmlformats.org/drawingml/2006/main'
    z.writestr(f'xl/worksheets/_rels/sheet{index}.xml.rels',f'<Relationships xmlns="{REL}"><Relationship Id="chart1" Type="{OFF}drawing" Target="../drawings/drawing1.xml"/></Relationships>')
    z.writestr('xl/drawings/_rels/drawing1.xml.rels',f'<Relationships xmlns="{REL}"><Relationship Id="rId1" Type="{OFF}chart" Target="../charts/chart1.xml"/></Relationships>')
    z.writestr('xl/drawings/drawing1.xml',f'<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" xmlns:a="{ans}"><xdr:twoCellAnchor><xdr:from><xdr:col>13</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>61</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from><xdr:to><xdr:col>23</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>79</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to><xdr:graphicFrame macro=""><xdr:nvGraphicFramePr><xdr:cNvPr id="2" name="Portfolio"/><xdr:cNvGraphicFramePr/></xdr:nvGraphicFramePr><xdr:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/></xdr:xfrm><a:graphic><a:graphicData uri="{cns}"><c:chart xmlns:c="{cns}" xmlns:r="{OFF[:-1]}" r:id="rId1"/></a:graphicData></a:graphic></xdr:graphicFrame><xdr:clientData/></xdr:twoCellAnchor></xdr:wsDr>')
    z.writestr('xl/charts/chart1.xml',f'<c:chartSpace xmlns:c="{cns}" xmlns:a="{ans}"><c:lang val="ja-JP"/><c:chart><c:title><c:tx><c:rich><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>{chart["title"]}</a:t></a:r></a:p></c:rich></c:tx></c:title><c:plotArea><c:layout/><c:pieChart><c:varyColors val="1"/><c:ser><c:idx val="0"/><c:order val="0"/><c:cat><c:strRef><c:f>{name}!{chart["labels"]}</c:f></c:strRef></c:cat><c:val><c:numRef><c:f>{name}!{chart["values"]}</c:f></c:numRef></c:val></c:ser></c:pieChart></c:plotArea><c:legend><c:legendPos val="r"/></c:legend><c:plotVisOnly val="1"/><c:dispBlanksAs val="zero"/></c:chart></c:chartSpace>')
