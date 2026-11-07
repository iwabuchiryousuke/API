"""Uploaded v3.6.1 worksheet appearance, with no uploaded cell data.

Only the public style table, theme, and row/column/view geometry were extracted.
No account, security, amount, original formula, relationship, or external link
from the attachment is stored here. Runtime uses the standard library only.
"""
import base64
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import re
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape, quoteattr
from zipfile import ZipFile, ZIP_DEFLATED
import zlib

NS='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL='http://schemas.openxmlformats.org/package/2006/relationships'
OFF='http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
LAYOUT='uploaded-template-v3.6.1'
CALENDAR_RED='FFFF7C80'
SOURCE_STYLES_SHA256='5d463e982ff8711adc73229e1af97133a91c8f17e9bbc9368c8dd1e0929147c9'
SOURCE_THEME_SHA256='6467cd8adf5c5cdb4f1ed32e23259c168ea23e45b75e2e0f57635b80952d664c'
_STYLES=(
    'c-rk+NpIUm6uuYee;{bImz3tdNRbp+q6h>?jUrZo0&Nc?awrW-a@66_ky7~LT-rZSpr>AY=>jxB7a09BM$)7ILT9*0lr1NYWXDz!'
    '0w!mMZ~5Mv$2X%lZatXBk#Gn}ig3~v)m=ptVB+HdC8M_Z>A;g5QAjCB0ubQ@w#6At#Rr}D-)p6G7Qurtgj8T6lC&+3DV;QBIrYad'
    '2I(%Iz=XXCF^K_XT{4o>34tI;Ia3_Tnxa^83{WB#5}L7p6(KPoUu6@?$MFPEG(-`ivs{)a#D4SPD8U2_BL;P<8o(E(szo$mN-8D!'
    '=mkA7@(E6HNOzfB8HXYA;YGL&xgi68A&E&|Pm*fNine6?l&mSqkO@3QeEP&rE6HLnrm5iLETL`Dnhy!Z<6*$&*k+z89NNPHpHg<o'
    'aV!UcD6gcqm+5yt+SyU=fBqN-Uw$DK9pw^EXPL9$xmOD4Mj3pqgvJWpAAX%-djIU?+ZVt5`0t<poSl4EjI0*nD9a*B_7{IWe<hcC'
    'j%!y@KB$Z?I{BS*%ca{ptq>;*@0(&V&}pSlg+mZAscNna#IWf10AbOKa~TEYhV<Gp{n^RSXD7c4XD7d%9zQ=l{^Ru7v(w{WIZX&+'
    '6wL}Moe%h9KvMQCmvWp-byzpA;=CCH6vI^5hfjpZI0ngi;c87#{@@dVqG&!5wpa{wS}bEwND_~Agt9%DP1y7%EV&h8a=O=0j|iBl'
    'nt64G6h|oFC`LUTVIq*xuq}F?>-M{z&BYAM7bpqf6tV!cj9hw6;)c4sUf*!78`ecx=7M~>1&X-mm0hR7F2ym}WviyGz9j_a0)fo{'
    'II871PQ%bu!_eN6zdgI>C~vAOf3Wx+ViK@Lxr$&-tcE(R2!@og6Eqt0C&d%415?TtiB2m(V1yG8aRn8}`kZBh0rFuK9q=Xpvv7_|'
    'XQ-xOEo<hPHkmh>DP^+|Q*`9ZnbdWdv`d(Zv{1)gSW!1XS<3=p8g789<Yok5GKpq;5gH{i<Zx7m22@?XDWk~e3C?F-$Tl!f2$&4u'
    'lvcJ)!*lv|X92*tDuDeg9zx>fYhC_2AN6oj>s=?vazC-9D7B4I^6&j-Fs}~3S0lb6nlUEmDU)5tvl+rG`*bbuT@&7NuY7w^aQ%MY'
    'JTcv9EGTaf3%51<H<+oe4f*QqrEgvK((ios(zh;q>06e)$~wZG-!U}a?Y?d@3LIr?k*iw}8K$+yCSf(m+gjd?txIgTY{xZXHs4{s'
    'wRlnPwC`7MLahJAPbljSZBOgfpF5wxo$Xo6<+5s6D}pYSK<A?ZKeA|x`y60Yf#+6`W`<bv8>PG?_Gd~}tGHFyQGQf%PUOp<et7x!'
    '_lv@#Fw9UyQ8F(>u4WLlnhWPBy8XV|w@ppz+NvTMPS2J04BL=&d(YKW*ENkj^Juwv%4`p&i@GWIY`|T!ZfU${_xknH_=Oj%i{rUY'
    'erI?nKjdAW4Ca>rh9HaRVE(c#F4`Z%0A;Z@r}_jPVp>uy+K+g7yf~-uIXI>(CRwj+`m1aFa*af;*jOArN@@NSGJ=@v`(3-?_B~B<'
    'l&&KgIy9w**>xq;=yhG!Ybcu1JF1;jZ*WeXA1<@cPBofo#HdK=tJ3y^#aLUcbqecqlNjte_J(FDdnSt*PghjQuz({umTpR(scNoe'
    'bo-`f*07svo@XnvsxH8rO^RX|p#(0&AJj*S6T(**Ay+dYFaB$x^B?bv?E3'
)
_THEME=(
    'c-p;J+j5&Q5Pk1V|AFDXkpOe^#LgtK-99AKPLqCs4C0aq;X!g7|9yqPAPnTv#KkkVRyw<T*j=sU^NZ(<XivCcX-;RlaRzk_(;yF1'
    '8qf4U_p5K7t}%{ih)7QHOy6UsU(COLIY(2T;0$X*pRy^M=?UkXDF7@G(}<nr8%)JolouJ|LM&ns7HB8zW(1fJIv_(S)isJTVfH#f'
    '{QIwaeSWaJB;t#5R!#(@xC^ASJ~(_d${)68*QC&%5Si)1N0{&K@iW&of*2PIGabrD2lI2#C=ouQtLUxdQ|r|V;iD<_ig?{9!lmg='
    'jK;oM+3(|)UioS4NdO80@v24-YuFC-%o?kJs^Nfb-!-f*Ua@cW?fcG^IqBf77C-5~<H}zy><)g?;_V*(5}H@O)xp~>-s#~d%S+c>'
    'cJLCEkd!|5(T?kRPK|CLQBHmg!hOeq?xKdbD4>*ArB8XM*nUP)8emy#8G6Wzl>o@(BA!x>?>9I?L0OcDr0XKpZqhj6r5iNG=;if*'
    'y|fOF9A+tf)%&>LagPUIt$Cn^K?Wt`qZ=?v3Ay8Yf^V2)#&VK|D={ersnz6QlZavMuY-#V)Q(wgDQ4OxXQJ%%QB$&Aq?A{4j$3bW'
    '5ua^#n}>Co8?c#Yh`005Zd*cQUP>`j3D{EqjU-%am_+03n1uB<5(az^3(t5b3;!h+K*@^OHX14q2YXU6(pZ294$C}M`kiEcN@D%V'
    '$lI77i|HE@%MujggIPF2W2C+e_<5;Sc(~pm4c{MTVZu|S7W8*Mp<PMamEPUMoBc~)*c6N}5KAhUTsj<`RB_^qvOcvFvkc0qoPgu#'
    ';V8lZADL-Iu~r!wT>T)p6!I;{#a$Bav^Cilzmcc~+ciYAwJ>G;5PBgLZPoPe4edN4n*`O}THT}-T#g%;3RlVIm^l~<(>h*##)0&@'
    '(ou&R>8tMO<~zap=eI|X(k^`Oea+K`CpZ-q5-PQ^hH4E{vrf}c9qT`L&f_8l$`wmWy}v;D`au->LfqW8gtO|F_sk1)eYm?en_H^R'
    '{{o{1YC`'
)
_GEOMETRY=(
    'c-rNf%Wfn!6oy}=)oavg+sBt04cQ<@uwaB32{w_UPEtv?rjrWYHDn;lk|kn+;A!A-vfwQ!yDmPRq-#=@-z#R3@u|z-cEz8Ql)u~Q'
    '{qKL>|NZCm)#P@%UFLcD{VKoNR^{|{EDvLQQZ7#3<?H;k%+EeQnu??8<d>qje0RE7=T&VLKk&BLTrSqtVUtydZ;H+PlcIR<W4`t`'
    '&yS{6atK=$SCd8c-%t4$`P$#{R#0`z_Qy?r-c|>PuMfI@@b>m_`q`xJ{Tu&@<+qzSfcD7bEI(g-SeM@wH(%wei)HD1uV+^KV1NH<'
    'v98DM8;Abu<Z^Y@jPY{uv1#3`&dQ~4`;l(TpTn^IiPI0;vbg-AJstWRLu3~sS%{t)sR!k#E=Tds3=g6%NV6b2ILCB3hUKW47?@+b'
    '9J|vWJ1oX^F^<J#gNK!MIoa-W9u_0!aq{rl;|6y~#2F;y&d_*{{B%y7L1JeLi3g8be>y78q0sEQv^%ifdvv^qPdqVy^kh%+ZTV(Z'
    'KJV$oX(;S&?BE9-r;u<vzz>g#vq)qw>y=~^$tc(VxDUb-<`=c=WpS04tJ7vhjOFCKxGKXctl9<edU26|v)Ej$uIhD=)suC8?!OW9'
    'nV&M7X8EkEvbYXyKc17Ktj5rl%Y1Q`Z$jxO=6O++v8+FIQ-q&3w^I?mhHi?moL?{Meu!5h^9PotujG6t=KNo{xPEr^x?U&TRYzrv'
    'l%PlzBNZr;VkChgb&S-YNE0IsDALAA3yO3x(t#qg7@2`0#hlIcIVzL`gd#{VganEq!5|VSiUh+*pg0l?B!MDHFq8y}CBa}4k7!aO'
    'Lm5uOp+Xr@A_Ij=MwFbRLK#s~phEc%C5Z}UL`j7TWkg9rg)*X~Mujq>q(Ox;q9lqa$%v9Dq9h|qqKHzAD1{<QF`^WTD8-0UD54bq'
    'MGr-kVnitvQHl|zP(&$4ltK}u7*PsElww3F6j6#1rBFl>BZ^Q&5hIFFL=hv3P(%?Uicmxm|KkZo6fvR*MHDfj2t^bzq6kG4F`@`X'
    '6fvR*MU-YlX%tbK5v5T?X-1Sr5v3VX8by?5L}?UJn!k`h5v3VX8by?5L}?UJnh~W@L}^BpMiFHgQ3genVMG}eQHBv^P(&F<ltB??'
    '7*PgAlwm{}6j6r1<U<i<7*PgAlwm{}6j6o|Wl%&}MwCSnWf@TxMU-VkSrk#05oJ+CSw@sa5oH-s7Dbe0L|GJ3mcKYh5oH-s7Dbe0'
    'L|GJ3juGWhL^(#3LlNZ|Q4U3vV?;R=QH~Mio**h*!vJx5&oykmJG18+HhtX_)A!C_bh)}ZZng%U_4VT8@oBNHN|9BiH%zLwRaLw3'
    'I{f9w%XVDB?5IwLJ<_<rDAa7;YzzitqS?_CZQ&n-ZQ7c({jh@4&}>`TI^8pqHJ4;{j&ApNTys3^%XEbdcj$H(By763yBAsLTiA2('
    'yW#CtLA_brzT=OTtH;w+S$8fxzkh%I?f#!Xo^SPVhsPO^!}Fz-%;5=D0*8lY65{YUki+9p4vzylJZ_}JV`c+mk}5oIXv`jiM-I*5'
    '3eWtxmWy~=`+4vz+-U2kG1rC+W1sg!gOm1XKULCTL1v!~qy~3tj{sy~{*QZ!{v*W;^&htO$d^e;QvH8TqIw?_Y7&i&>@kVPM)sIQ'
    'V<US^qOp-ZCehf)9+PNnWRFQSHnPVg8XMVb5{(Y+Gl@or_L)SZL;Fmk(V=}N(df`VlW25kpGh=2wAUn}6eba+Fo`IINkl13B1&Nr'
    'Q3{iYQkX=P!X%;;CK07Di716hL@7)nN?{UF3X_OZm_(GqB%%~15v4GRD1}KxDNG_tVG>aalZaB7M3llLq7)_(r7(#og-JvyOd?8Q'
    '5>X11h*FqDgffW`;t;|ULTEt<3kV_oARHfr(t|K~5W)_^&q3%o2>S*h+aTN;gi3=jXAq(c!izy@FbL}fA-N!&7KFlrFjf$P3c^Q0'
    '=qCu<1R<9oToQyjf-pr8V#vUZhl+&UQ6wZqMZ%fAiiF=d$0`zL_LL#v>Siz$WaB%`qwgT%)!`*^t8UbS1TjIP-eBw;3DumcKbbia'
    'hAR^8u_4jE=8KissU$Zm!W*%=tFo~)?(XjX1K%Sd#s'
)

def _unpack(value):return zlib.decompress(base64.b85decode(value))

GEOMETRY=json.loads(_unpack(_GEOMETRY))
HEADERS={
    '取引':{1:'日付',2:'口座',3:'銘柄',4:'コード',5:'預り',6:'売買',7:'数量',8:'単価',
          9:'約定・受取額',10:'手数料・税',11:'保有数',12:'平均単価',13:'確定損益',
          16:'成長枠残高',17:'積立枠残高',18:'現金(円)',19:'現金(ドル)'},
    '配当':{1:'日付',2:'口座',3:'銘柄',4:'コード',5:'預り',6:'分類',7:'数量',8:'単価(円)',9:'受取額(円)',
          11:'コード',12:'銘柄',13:'権利付き最終日',14:'配当金支払日',15:'本決算発表日',16:'株数',
          17:'DPS',18:'配当利回',19:'実績',20:'予想(税後)',21:'年間予想',22:'累計配当金',
          23:'末日終値',24:'1日終値'},
}


def apply_template_layout(sheets):
    """Expose the exact geometry in the model as well as in the final XLSX."""
    for sheet in sheets:
        geometry=GEOMETRY[sheet['name']]
        sheet.update(widths=dict(geometry['widths']),row_height=20.1,header_height=20.1,
                     row_heights={int(row):float(attrs.get('ht',geometry['default_row_height']))
                                  for row,attrs in geometry['rows'].items()},
                     default_row_height=geometry['default_row_height'],zoom=geometry['zoom'],
                     freeze=None,filter=None,merges=[],hidden_columns=[])
        by_position={(cell['r'],cell['c']):cell for cell in sheet['cells']}
        for column,value in HEADERS[sheet['name']].items():
            cell=by_position.get((1,column))
            if cell is None:
                cell=dict(r=1,c=column,k='text',v=value,fill=None);sheet['cells'].append(cell)
            else:cell.update(k='text',v=value)
        if sheet['name']=='配当':
            total=sheet.get('blocks',{}).get('summary_last_row')
            if total:
                for column in (23,24):
                    if (total,column) not in by_position:
                        sheet['cells'].append(dict(r=total,c=column,k='number',v=None,fill=None))
    return sheets


def _style_id(sheet,cell):
    """Select the original template style by its financial presentation role."""
    if 'template_style' in cell:
        selected=cell['template_style']
        if isinstance(selected,bool) or not isinstance(selected,int) or not 0<=selected<30:
            raise ValueError('Invalid template style')
        return selected
    row,column=cell['r'],cell['c']
    if row==1:
        if sheet['name']=='取引':return {12:28,13:29,15:10}.get(column,8)
        return 8
    if sheet['name']=='取引':
        if column==15:return 1
        if column>=16:return 3
        if column in (11,12):return 2
        if column==13:return 15
        variant={'DDEBF7':'buy','FCE4D6':'sell'}.get(cell.get('fill'),'plain')
        if column==1:return {'buy':7,'sell':14,'plain':10}[variant]
        if column==4:return {'buy':6,'sell':13,'plain':9}[variant]
        if column<=6:return {'buy':5,'sell':12,'plain':8}[variant]
        if column<=10:return {'buy':4,'sell':11,'plain':3}[variant]
        return 1
    if column<=9:
        return 10 if column==1 else 9 if column==4 else 8 if column<=6 else 3
    blocks=sheet.get('blocks',{})
    summary_last=blocks.get('summary_last_row',2)
    cumulative_header=blocks.get('cumulative_header_row',summary_last+2)
    cumulative_total=blocks.get('cumulative_total_row',cumulative_header+2)
    grand=blocks.get('cumulative_last_row',cumulative_total+1)
    calendar_label=blocks.get('calendar_label_row',grand+2)
    calendar_last=blocks.get('calendar_last_row',calendar_label)
    if row<summary_last:
        if column==11:return 9
        if column in (12,13,14):return 8
        if column==15:return 1 if cell.get('v') in (None,'','未定','無配') else 20
        if column==18:return 19
        return 3
    if row==summary_last:
        return {11:8,19:22,20:25,21:25,22:23,23:3,24:3}.get(column,0)
    if row==cumulative_header:return 1 if column==14 else 8 if column in (11,12,13) else 0
    if cumulative_header<row<cumulative_total:
        if column==11:return 18
        if column==12:return 21 if row==cumulative_total-1 else 17
        if column==13:return 17
        if column==14:return 3
        return 0
    if row==cumulative_total:return {11:8,12:24,13:17,14:3}.get(column,0)
    if row==grand:return {11:8,12:17}.get(column,0)
    if row==calendar_label:return 8 if column==11 else 0
    if calendar_label<row<=calendar_last:
        return 10 if column==11 else 8 if column in (12,13) else 16 if column==14 else 0
    return 0


class _StyleTable:
    def __init__(self):
        self.original=_unpack(_STYLES).decode('utf-8')
        root=ET.fromstring(self.original)
        self.xfs=list(root.find('{'+NS+'}cellXfs'))
        self.formats={node.get('formatCode'):int(node.get('numFmtId'))
                      for node in root.find('{'+NS+'}numFmts')}
        self.extra_formats={};self.extra_styles=[];self.clones={}

    def style(self,sheet,cell):
        base=_style_id(sheet,cell)
        blocks=sheet.get('blocks',{})
        calendar=blocks.get('calendar_label_row',10**9)<cell['r']<=blocks.get('calendar_last_row',-1)
        fmt=cell.get('format')
        if not calendar:return base
        if cell['c']==11 and fmt=='m/d':return self.clone(base,'m/d')
        if cell['c']==14 and isinstance(fmt,str) and '予想' in fmt:
            return self.clone(base,'#,##0"（予想）"')
        return base

    def clone(self,base,fmt):
        key=(base,fmt)
        if key in self.clones:return self.clones[key]
        fmt_id=self.formats.get(fmt,self.extra_formats.get(fmt))
        if fmt_id is None:
            fmt_id=max([181]+list(self.formats.values())+list(self.extra_formats.values()))+1
            self.extra_formats[fmt]=fmt_id
        cloned=deepcopy(self.xfs[base]);cloned.set('numFmtId',str(fmt_id));cloned.set('applyNumberFormat','1')
        self.extra_styles.append(ET.tostring(cloned,encoding='unicode'))
        result=len(self.xfs)+len(self.extra_styles)-1;self.clones[key]=result
        return result

    def xml(self):
        result=self.original
        formats=''.join('<numFmt numFmtId="'+str(value)+'" formatCode='+quoteattr(key)+'/>'
                        for key,value in self.extra_formats.items())
        if formats:
            result=result.replace('</numFmts>',formats+'</numFmts>')
            result=re.sub(r'(<numFmts count=")(\d+)',lambda match:match[1]+str(int(match[2])+len(self.extra_formats)),result,count=1)
        if self.extra_styles:
            result=result.replace('</cellXfs>',''.join(self.extra_styles)+'</cellXfs>')
            result=re.sub(r'(<cellXfs count=")(\d+)',lambda match:match[1]+str(int(match[2])+len(self.extra_styles)),result,count=1)
        # The user's explicit red is the template's manually filled calendar
        # row, not the old single-nearest-event pale conditional fill.
        red='<dxf><fill><patternFill patternType="solid"><fgColor rgb="'+CALENDAR_RED+'"/><bgColor rgb="'+CALENDAR_RED+'"/></patternFill></fill></dxf>'
        result=result.replace('</dxfs>',red+'</dxfs>')
        result=re.sub(r'(<dxfs count=")(\d+)',lambda match:match[1]+str(int(match[2])+1),result,count=1)
        return result


def _column(number):
    result=''
    while number:
        number,remainder=divmod(number-1,26);result=chr(65+remainder)+result
    return result


def _clean(value):return ''.join(character for character in str(value) if ord(character)>=32 or character in '\t\n\r')


def _value(cell):
    value=cell.get('v');kind=cell.get('k','text')
    if value is None or value=='':return '', ''
    if kind.startswith('formula_'):
        expression=str(value).lstrip('=')
        if re.search(r'\[[^\]]+\](?:[^!]*!)',expression):
            raise ValueError('External workbook references are not copied from the template')
        return '', '<f>'+escape(expression)+'</f>'
    if kind=='date':
        day=value.date() if isinstance(value,datetime) else value if isinstance(value,date) else date.fromisoformat(value)
        return '', '<v>'+str((day-date(1899,12,30)).days)+'</v>'
    if isinstance(value,(int,float,Decimal)) and not isinstance(value,bool):
        if not math.isfinite(value):raise ValueError('The workbook contains a nonfinite number')
        return '', '<v>'+str(value)+'</v>'
    return ' t="inlineStr"','<is><t xml:space="preserve">'+escape(_clean(value))+'</t></is>'


def _sheet_xml(sheet,styles):
    geometry=GEOMETRY[sheet['name']]
    rows={}
    for cell in sheet['cells']:
        row,column=cell['r'],cell['c']
        if not 1<=row<=1048576 or not 1<=column<=geometry['max_col']:
            raise ValueError('A cell lies outside the uploaded template columns')
        attributes,content=_value(cell)
        style=styles.style(sheet,cell)
        rows.setdefault(row,{})[column]=f'<c r="{_column(column)}{row}" s="{style}"{attributes}>{content}</c>'
    populated_rows=set(rows)
    if sheet['name']=='取引':
        # These four blank numeric cells are the template's only styled tail.
        # Keep its empty grid extent without carrying its original formulas.
        for row in range(84,88):
            if row not in rows:rows[row]={11:f'<c r="K{row}" s="3"/>'}
    maximum=max([geometry['min_rows']]+list(rows))
    data=[]
    for row in range(1,maximum+1):
        attrs=dict(geometry['rows'].get(str(row),{}))
        attrs['r']=str(row)
        if row in populated_rows or row>geometry['min_rows']:
            attrs.update(ht='20.100000000000001',customHeight='1')
        content=''.join(rows.get(row,{}).get(column,'') for column in sorted(rows.get(row,{})))
        data.append('<row'+''.join(' '+key+'='+quoteattr(value) for key,value in attrs.items())+'>'+content+'</row>')
    rules=[]
    for priority,rule in enumerate(sheet.get('conditional_formats',[]),1):
        # Only the explicit calendar region is allowed to gain a red fill.
        rules.append('<conditionalFormatting sqref='+quoteattr(rule['range'])+'><cfRule type="expression" dxfId="1" priority="'+str(priority)+'" stopIfTrue="1"><formula>'+escape(rule['formula'])+'</formula></cfRule></conditionalFormatting>')
    return ('<worksheet xmlns="'+NS+'" xmlns:r="'+OFF[:-1]+'">'
            '<dimension ref="A1:'+_column(geometry['max_col'])+str(maximum)+'"/>'
            +geometry['sheetViews']+geometry['sheetFormatPr']+geometry['cols']
            +'<sheetData>'+''.join(data)+'</sheetData>'+geometry['phoneticPr']
            +''.join(rules)+geometry['pageMargins']+'</worksheet>')


def write_template_model(model,target):
    """Write current ledger values inside the uploaded workbook's appearance."""
    target=Path(target);temporary=target.with_suffix('.tmp')
    if [sheet['name'] for sheet in model['sheets']]!=['取引','配当']:
        raise ValueError('The uploaded template contains exactly two sheets')
    styles=_StyleTable()
    sheets=[_sheet_xml(sheet,styles) for sheet in model['sheets']]
    try:
        with ZipFile(temporary,'w',ZIP_DEFLATED) as package:
            types=[('/xl/workbook.xml','sheet.main'),('/xl/styles.xml','styles'),
                   ('/xl/worksheets/sheet1.xml','worksheet'),('/xl/worksheets/sheet2.xml','worksheet')]
            overrides=''.join('<Override PartName="'+path+'" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.'+kind+'+xml"/>' for path,kind in types)
            package.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'+overrides+'<Override PartName="/xl/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/></Types>')
            package.writestr('_rels/.rels','<Relationships xmlns="'+REL+'"><Relationship Id="rId1" Type="'+OFF+'officeDocument" Target="xl/workbook.xml"/></Relationships>')
            entries=''.join('<sheet name='+quoteattr(sheet['name'])+' sheetId="'+str(index)+'" r:id="rId'+str(index)+'"/>' for index,sheet in enumerate(model['sheets'],1))
            package.writestr('xl/workbook.xml','<workbook xmlns="'+NS+'" xmlns:r="'+OFF[:-1]+'"><sheets>'+entries+'</sheets><calcPr calcId="191029" calcMode="auto" fullCalcOnLoad="1" forceFullCalc="1"/></workbook>')
            rels=''.join('<Relationship Id="rId'+str(index)+'" Type="'+OFF+'worksheet" Target="worksheets/sheet'+str(index)+'.xml"/>' for index in (1,2))
            package.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="'+REL+'">'+rels+'<Relationship Id="styles" Type="'+OFF+'styles" Target="styles.xml"/><Relationship Id="theme" Type="'+OFF+'theme" Target="theme/theme1.xml"/></Relationships>')
            package.writestr('xl/styles.xml',styles.xml())
            package.writestr('xl/theme/theme1.xml',_unpack(_THEME))
            for index,sheet in enumerate(sheets,1):package.writestr('xl/worksheets/sheet'+str(index)+'.xml',sheet)
        os.replace(temporary,target)
    finally:temporary.unlink(missing_ok=True)
    return target
