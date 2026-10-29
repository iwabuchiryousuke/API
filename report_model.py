"""Presentation adapter: stable cost engine, simple visible sheets, private helpers."""
from report_legacy import *
from report_legacy import build as legacy_build
import re
from security_names import load as load_names
GAIN='[Red]+#,##0;[Blue]-#,##0;0'

def short(value):
    return {'母(雅美)':'母','父(慶太)':'父','買':'B','売':'S','税還付':'税還','NISA(成)':'N-成','NISA(積)':'N-積','NISA（成）':'N-成','NISA（積）':'N-積','N(成)':'N-成','N(積)':'N-積','特定':'特'}.get(value,value)

def build_legacy(store,activities,month='',management=False,today=None):
    model=legacy_build(store,activities,month,management)
    t,d=model['sheets'][:2]
    original={(c['r'],c['c']):dict(c) for c in t['cells']}
    helper={'name':'計算明細','cells':[dict(c) for c in t['cells'] if c['c']>=22], 'widths':t['widths'].copy(),'merges':[],'filter':None,'chart':None,'hidden':True}
    t['cells']=[c for c in t['cells'] if c['c']<22]
    # Remap original formulas before adding the new visible table.
    for sh in model['sheets']:
        sh['filter']=None
        for c in sh['cells']:
            if c['k'].startswith('formula_'):
                c['v']=re.sub(r'取引!(\$?(?:V|W|X|Y|Z|AA|AB|AC|AD|AE|AF|AG|AI|AJ|AK|AL|AM|AN|AO|AP|AQ|AR|AS|AT|AU|AV)\$?\d+)',r'計算明細!\1',c['v'])
    model['sheets'].append(helper)
    for sh in model['sheets']:
        for c in sh['cells']:
            if isinstance(c['v'],str):
                if c['k'].startswith('formula_'):
                    for old,new in (('買','B'),('売','S'),('税還付','税還'),('NISA(成)','N-成'),('NISA(積)','N-積'),('特定','特')):
                        c['v']=c['v'].replace('"'+old+'"','"'+new+'"')
                else:c['v']=short(c['v'])
    def put(sh,r,c,v=None,k=None,fill=None,fmt=None):
        cell=dict(r=r,c=c,v=v,k=k or ('number' if isinstance(v,(int,float)) else 'text'),fill=fill)
        if fmt:cell['format']=fmt
        sh['cells'].append(cell)
    def formula(sh,r,c,v,kind='number',**kw):put(sh,r,c,v,'formula_'+kind,**kw)
    names={}
    for sh in (t,d):
        cells={(c['r'],c['c']):c for c in sh['cells']}
        rows=sorted({r for r,c in cells if c==1 and r>1})
        for r in rows:
            if (r,4) not in cells:continue
            ident=cells[r,4]['v']
            if ident not in ('',None):names[str(ident)]=cells.get((r,3),{}).get('v','')
            if sh is t and cells.get((r,6),{}).get('v')=='分割':
                cells[r,6]['v']='B'
                for c in range(1,11):
                    if (r,c) in cells:cells[r,c]['fill']=None
                for c in (8,9,10):cells[r,c].update(v=0,k='number')
            formula(sh,r,3,f'IFERROR(VLOOKUP(D{r},配当!$L$2:$M$10000,2,FALSE),"")','text',fill=cells.get((r,3),{}).get('fill'))
    owned_codes=set(names)
    names.update(load_names(store.directory))
    put(d,1,6,'分類');put(d,1,12,'コード');put(d,1,13,'表示銘柄名')
    for r,(ident,name) in enumerate(sorted(names.items()),2):
        put(d,r,12,code(ident),'code');put(d,r,13,name)
    for c in d['cells']:
        if c['c']==21 and c['r']>1 and (c['r'],20) in {(x['r'],x['c']) for x in d['cells']}:
            c.update(v=f'IFERROR(VLOOKUP(T{c["r"]},$L$2:$M$10000,2,FALSE),"")',k='formula_text')
    # Corrected reference moves the dividend summary from T:AE to O:Z.
    # The calculation rows remain at AG:AQ; move only the visible summary.
    forecast_header=next((c['r'] for c in d['cells'] if c['c']==36 and c['v']=='権利最終日'),100000)
    def shift_ref(match):
        prefix,letters,dollar,row=match.groups();n=0
        for ch in letters:n=n*26+ord(ch)-64
        if 20<=n<=36 and int(row)<forecast_header:return prefix+col(n-5)+dollar+row
        return match.group(0)
    def shift_formula(f):
        # Preserve other-sheet references and string literals.
        parts=re.split(r'("(?:[^"]|"")*"|(?:取引|計算明細|配当)![A-Z$0-9:]+)',f)
        for i,part in enumerate(parts):
            if i%2==0:parts[i]=re.sub(r'(?<![A-Za-z0-9_!])(\$?)([A-Z]{1,3})(\$?)([0-9]+)',shift_ref,part)
        return ''.join(parts)
    for c in d['cells']:
        if 20<=c['c']<=36 and c['r']<forecast_header:
            c['c']-=5
            if c['k'].startswith('formula_'):c['v']=shift_formula(c['v'])
    d['hidden_columns']=list(range(33,44))
    # A full closing price must belong to the previous month's last trading day.
    day=date.fromisoformat(model['as_of']);prev=day.replace(day=1)-timedelta(days=1)
    from market_data import load_research
    facts=load_research(store.directory,day.year).get('securities',{})
    headers=['口座','コード','銘柄','預り','保有数量','平均取得単価','取得総額','確定損益','前月末株価','評価額','評価損益','確認']
    for c,h in enumerate(headers,22):put(t,1,c,h)
    put(helper,1,34,'評価損益未確認（1=未確認）')
    state_rows=sorted({r for r,c in original if c==22 and r>1})
    ids=[]
    for r in state_rows:
        raw=lambda c:original.get((r,c),{}).get('v')
        ident=str(raw(23));ids.append(ident);q=raw(26) or 0
        fill='DDEBF7' if q>0 else None
        for c in (22,23,25,26,27,28,29):
            value=short(raw(c));put(t,r,c,value,'code' if c==23 else None,fill,GAIN if c==29 else None)
        formula(t,r,24,f'IFERROR(VLOOKUP(W{r},配当!$L$2:$M$10000,2,FALSE),"")','text',fill=fill)
        f=facts.get(ident,{})
        close=f.get('close') if str(f.get('close_date',''))[:7]==prev.isoformat()[:7] else None
        put(t,r,30,close,'number',fill)
        put(helper,r,34,1 if raw(30) or (q>0 and close is None) else 0)
        formula(t,r,31,f'IF(Z{r}=0,0,IF(ISNUMBER(AD{r}),Z{r}*AD{r}/計算明細!AF{r},""))',fill=fill)
        formula(t,r,32,f'IF(計算明細!AH{r}>0,"",IF(Z{r}=0,0,IF(AND(ISNUMBER(AE{r}),ISNUMBER(AB{r})),AE{r}-AB{r},"")))',fill=fill,fmt=GAIN)
        reason=raw(33) or '履歴から計算'
        if close is None and q>0:reason+='／前月末株価未確認'
        put(t,r,33,reason,'text',fill)
    # Aggregate remaining cost, not the arithmetic mean of account averages.
    for c,h in enumerate(['コード','銘柄','保有株数','平均取得単価','評価額','確定損益','評価損益','配当金','合計利益','確認'],12):put(t,30,c,h)
    for r,ident in enumerate(sorted(set(ids)|owned_codes),31):
        put(t,r,12,code(ident),'code');formula(t,r,13,f'IFERROR(VLOOKUP(L{r},配当!$L$2:$M$10000,2,FALSE),"")','text')
        formula(t,r,14,f'SUMIFS($Z$2:$Z$10000,$W$2:$W$10000,L{r})')
        formula(t,r,15,f'IF(OR(N{r}<=0,SUMIFS(計算明細!$AD$2:$AD$10000,計算明細!$W$2:$W$10000,L{r})>0),"",SUMIFS($AB$2:$AB$10000,$W$2:$W$10000,L{r})/N{r}*IFERROR(VLOOKUP(L{r},計算明細!$W$2:$AF$10000,10,FALSE),1))')
        formula(t,r,16,f'IF(SUMIFS(計算明細!$AH$2:$AH$10000,計算明細!$W$2:$W$10000,L{r})>0,"",SUMIFS($AE$2:$AE$10000,$W$2:$W$10000,L{r}))')
        for c,src,flag in ((17,'AC','AE'),(18,'AF','AH')):
            formula(t,r,c,f'IF(SUMIFS(計算明細!${flag}$2:${flag}$10000,計算明細!$W$2:$W$10000,L{r})>0,"",SUMIFS(${src}$2:${src}$10000,$W$2:$W$10000,L{r}))',fmt=GAIN)
        formula(t,r,19,f'SUMIFS(配当!$I$2:$I$10000,配当!$D$2:$D$10000,L{r},配当!$F$2:$F$10000,"配当")')
        formula(t,r,20,f'IF(COUNT(Q{r}:S{r})=3,SUM(Q{r}:S{r}),"")',fmt=GAIN)
        formula(t,r,21,f'IF(COUNT(Q{r}:S{r})=3,"参考値","原価・株価要確認")','text')
    put(t,24,12,'無色・0円のBは株式分割の追加株数。原価総額・NISA買付額は増えません。')
    put(t,25,12,'平均取得単価は残存原価÷保有数量（投信は1万口）。評価額は現在の株数×前月末株価。')
    put(t,26,12,'確定損益は売却益。銘柄別の配当金は全登録期間の税引後入金。未確認の原価・株価は空欄。')
    layout=json.loads((Path(__file__).parent/'reference'/'excel_layout.json').read_text(encoding='utf-8'))
    for sh in model['sheets']:
        sh['cells']=list({(c['r'],c['c']):c for c in sh['cells']}.values())
        sh['filter']=None
        if sh['name'] in layout:
            spec=layout[sh['name']];sh['widths'].update(spec['widths']);sh['row_height']=spec['row_height'];sh['header_height']=spec['header_height']
            for c in sh['cells']:c['align']=(spec.get('header_align',spec['align']) if c['r']==1 else spec['align']).get(str(c['c']),{'v':'center','wrap':True})
    # Move the visible dividend tables left; the editable name dictionary is last.
    def mapped(c,r):
        if c in (12,13):return c+18
        return c-3 if 15<=c<=31 and r<forecast_header else c
    ref_pattern=re.compile(r'(?:(取引|配当|計算明細)!)?(\$?)([A-Z]{1,3})(\$?)([0-9]+)(?::(\$?)([A-Z]{1,3})(\$?)([0-9]+))?')
    def remap_formula(f,sheet):
        def sub(m):
            target=m[1] or sheet
            if target!='配当':return m[0]
            def one(d,letters,rd,row):
                n=0
                for ch in letters:n=n*26+ord(ch)-64
                return d+col(mapped(n,int(row)))+rd+row
            ans=(m[1]+'!' if m[1] else '')+one(*m.groups()[1:5])
            if m[7]:ans+=':'+one(*m.groups()[5:9])
            return ans
        parts=re.split(r'("(?:[^"]|"")*")',f)
        return ''.join(ref_pattern.sub(sub,x) if i%2==0 else x for i,x in enumerate(parts))
    diff_rows={c['r'] for c in d['cells'] if c['v']=='銘柄別との差'}
    d['cells']=[c for c in d['cells'] if not(c['r'] in diff_rows and 15<=c['c']<=31)]
    for sh in model['sheets']:
        for c in sh['cells']:
            if c['k'].startswith('formula_'):c['v']=remap_formula(c['v'],sh['name'])
    for c in d['cells']:
        old=c['c'];c['c']=mapped(old,c['r'])
        if c['v']=='年' and old==16:c['v']=''
        if old==16 and isinstance(c['v'],int) and 2024<=c['v']<=2100:
            c['format']='0"年"';c['align']={'h':'center','v':'center'}
        if c['v'] in ('DPS(税引前)','DPS（税引前）'):c['v']='DPS'
        if c['v']=='配当利回り':c['v']='配当利回'
        if c['v']=='予想(税引後参考)':c['v']='予想(税引後)'
        if c['v']=='実績(税引後)':c['v']='実績'
        if c['v']=='配当(税引後)':c['v']='配当'
    t['widths'].update({str(k):v for k,v in {1:13,12:8,13:18,14:12,15:16,16:15,17:15,18:17,19:17,20:25,21:23,22:8,23:8,24:18,25:8,26:12,27:16}.items()})
    old_widths=d['widths'].copy()
    for c in range(15,32):d['widths'][str(c-3)]=old_widths.get(str(c),14)
    d['widths'].update({str(k):v for k,v in {12:8,13:18,18:8,19:12,20:14,21:18,22:14,23:15,29:3,30:8,31:20}.items()})
    for sh,cols in ((t,{12,13,22,23,24,25}),(d,{12,13,30,31})):
        for c in sh['cells']:
            if c['c'] in cols:c['align']={'h':'center','v':'center','wrap':True}
    # Actual dates retain a typed Excel value; each custom number format is distinct.
    for c in t['cells']:
        if c['c']==12 and c['r'] in (1,11):c.update(k='date',format='m/d')
    holdings=sorted((c for c in t['cells'] if c['c']==22 and c['r']>1),key=lambda c:c['r'])
    boundaries={cur['r'] for prev,cur in zip(holdings,holdings[1:]) if prev['v']!=cur['v']}
    for c in t['cells']:
        if c['r'] in boundaries and 22<=c['c']<=33:c['border_top']=True
    d['widths']['1']=13
    for c in d['cells']:
        if c['v']=='配当利回':c['v']='配当利回り'
        if c.get('format')=='0"年"':c['format']='[Black]0"年"'
    from date_highlights import rules_for
    d['conditional_formats']=[]
    for c in d['cells']:
        if c['c'] in (14,16) and 1<c['r']<forecast_header and isinstance(c['v'],str) and not c['k'].startswith('formula_'):
            d['conditional_formats'].extend(rules_for(c,today))
    # Current observations are separately dated; never fill a past month-end with them.
    from sbi_views import latest
    observations=latest(store,model['as_of'])
    base=max([c['r'] for c in t['cells'] if 12<=c['c']<=21]+[40])+3
    put(t,base,12,'SBI取得時点の資産（指定日の資産とは別）')
    for c,h in enumerate(['口座','国内株式等','外国株式(円)','現金合計','外貨現金(円)','合計(円)','成長枠残高','積立枠残高','取得日・確認'],12):put(t,base+1,c,h)
    for r,a in enumerate(store.accounts(),base+2):
        summary=observations.get((a['id'],'口座サマリー'),{});asset=observations.get((a['id'],'My資産'),{})
        v=asset.get('values',{});sv=summary.get('values',{})
        from asset_snapshots import combine_assets
        amounts=combine_assets(v)
        cash=sv.get('cash_total') if sv.get('cash_total') is not None else amounts.get('cash')
        parts=[amounts['domestic'],amounts['foreign'],cash,amounts['foreign_cash']]
        total=sum(parts) if all(x is not None for x in parts) else None
        stamp=asset.get('observed_at','')[:10] or summary.get('observed_at','')[:10]
        values=[a['label'],*parts,total,sv.get('growth_remaining',{}).get('remaining'),sv.get('tsumitate_remaining',{}).get('remaining'),'父は手動' if a['id']=='E' else stamp+' 空欄は未取得']
        for c,value in enumerate(values,12):put(t,r,c,value)
    model['version']='3.5.11'  
    return model


def build(store,activities,month='',management=False,today=None):
    """The v3.6 layout requested in the current workbook specification."""
    from report_v36 import build as current_build
    return current_build(store,activities,month,management,today)
