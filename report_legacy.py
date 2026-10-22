"""Shared, typed report model for the desktop exporter and review workbook.
No existing ledger payload/signature is rewritten. Formula inputs remain numeric.
"""
import calendar,json,hashlib
from datetime import date,timedelta
from decimal import Decimal
from pathlib import Path
from collections import defaultdict
from cost_basis import calculate,display_average,instrument_id
from custody_labels import trade_label,event_label

HEADERS=['日付','口座','銘柄','コード','預り','売買','数量','単価（円）','約定・受取額（円）','手数料・税額（円）']
NUM='#,##0;[Red]-#,##0;0'

def col(n):
    s=''
    while n:n,r=divmod(n-1,26);s=chr(65+r)+s
    return s

def number(v):return None if v in ('',None) else float(Decimal(str(v)))
def code(v):return int(v) if str(v).isdigit() else str(v)

def reporting_code(record):
    raw=str(record.get('code','')).strip()
    return raw or 'F-'+hashlib.sha256(instrument_id(record).encode()).hexdigest()[:10].upper()
def cutoff(month):
    if not month:return date.today().isoformat()
    y,m=map(int,month.split('-'));return f'{month}-{calendar.monthrange(y,m)[1]:02}'

class Sheet:
    def __init__(self,name):self.name=name;self.cells=[];self.widths={};self.chart=None;self.filter=None;self.merges=[]
    def put(self,row,column,value=None,kind=None,fill=None):
        if isinstance(value,Decimal):value=float(value)
        if kind is None:kind='number' if isinstance(value,(float,int)) else 'text'
        self.cells.append(dict(r=row,c=column,v=value,k=kind,fill=fill))
    def row(self,row,values,start=1,fill=None):
        for c,v in enumerate(values,start):self.put(row,c,v,fill=fill)
    def formula(self,r,c,f,kind='number'):self.put(r,c,f,kind='formula_'+kind)
    def result(self):
        self.cells=list({(c["r"],c["c"]):c for c in self.cells}.values())
        return vars(self)

def qty_formula(code_ref,account_ref=None,day_ref=None,custody_ref=None):
    def s(side):
        a=f'SUMIFS(取引!$G$2:$G$10000,取引!$D$2:$D$10000,{code_ref},取引!$F$2:$F$10000,"{side}"'
        if account_ref:a+=f',取引!$B$2:$B$10000,{account_ref}'
        if day_ref:a+=f',取引!$A$2:$A$10000,"<="&{day_ref}'
        if custody_ref:a+=f',取引!$E$2:$E$10000,{custody_ref}'
        return a+')'
    return s('買')+'-'+s('売')+'+'+s('分割')

def cash_formula(code_ref,year=None,account_ref=None):
    f=f'SUMIFS(配当!$I$2:$I$10000,配当!$D$2:$D$10000,{code_ref},配当!$F$2:$F$10000,"配当"'
    if year:f+=f',配当!$A$2:$A$10000,">="&DATE({year},1,1),配当!$A$2:$A$10000,"<"&DATE({year}+1,1,1)'
    else:f+=',配当!$A$2:$A$10000,">="&DATE(2024,1,1)'
    if account_ref:f+=f',配当!$B$2:$B$10000,{account_ref}'
    return f+')'

def build(store,activities,month='',management=False):
    store.records(month)
    day=cutoff(month);year=int(day[:4]);start=date.fromisoformat(day).replace(day=1);prev=start-timedelta(days=1)
    trades=store.records(month);events=activities.events(month)
    if len(trades)+len(events)>9998:raise ValueError('1回のExcel出力は9998明細までです。集計範囲を拡張してから出力してください。')
    # All-period exports include future-dated records if present, with matching cost cutoff.
    if not month:day=max([day]+[t['trade_date'] for t in trades]+[e['date'] for e in events]);year=int(day[:4])
    start=date.fromisoformat(day).replace(day=1);prev=start-timedelta(days=1)
    detail,states=calculate(store,activities,day)
    names={reporting_code(t):t['name'] for t in trades}
    names.update({str(e['code']):e['name'] for e in events if e.get('code')})
    from market_data import load_research
    research=load_research(store.directory,year)
    facts=research.get('securities',{})
    # Public reference records are not evidence that the user owns a security.
    if any(e['type']=='配当' and not e.get('code') for e in events):names['未分類']='コード未登録の配当'
    t=Sheet('取引');d=Sheet('配当')
    for sh in (t,d):
        sh.row(1,HEADERS);sh.widths={str(i):w for i,w in enumerate([16,12,44,10,18,10,13,18,23,23],1)}
    rows=[]
    for x in trades:
        rows.append((x['trade_date'],[x['trade_date'],x['account_label'],x['name'],code(reporting_code(x)),trade_label(x,trades),x['side'],number(x['quantity']),number(x['price']),number(x['settlement_amount']),number(Decimal(x['fees'])+Decimal(x['tax']))]))
    for x in events:
        if x['type']=='株式分割':
            alloc=detail.get(('split',x['account'],x['code'],x['date']),{}).get('allocations') or {'預り未確認':[x['before'],x['added'],x['after']]}
            for bucket,amounts in alloc.items():
                rows.append((x['date'],[x['date'],x['account_label'],x['name'],code(x['code']),bucket,'分割',number(amounts[1]),None,None,None]))
    if len(rows)>9998:raise ValueError('分割明細を含めて9998行を超えます。式範囲の拡張が必要です。')
    for r,(_,values) in enumerate(sorted(rows,key=lambda x:x[0],reverse=True),2):
        for c,v in enumerate(values,1):t.put(r,c,v,'date' if c==1 else 'code' if c==4 else None,'DDEBF7' if values[5]=='買' else 'FCE4D6' if values[5]=='売' else None)
    for r,e in enumerate(sorted([e for e in events if e['type'] in ('配当','税還付','利息')],key=lambda x:x['date'],reverse=True),2):
        values=[e['date'],e['account_label'],e.get('name') or e.get('description'),code(e.get('code') or ('未分類' if e['type']=='配当' else '')),event_label(e,trades),e['type'],number(e.get('quantity')),number(e.get('per_share')),number(e.get('net',e.get('amount'))),number(e.get('tax'))]
        for c,v in enumerate(values,1):d.put(r,c,v,'date' if c==1 else 'code' if c==4 else None)
    t.filter=f'A1:J{max(2,len(rows)+1)}';d.filter=f'A1:J{max(2,1+sum(e["type"] in ("配当","税還付","利息") for e in events))}'
    # Typed helper tables; no ledger decisions are hidden in a cell formula.
    t.row(1,['口座','コード','銘柄','預り','数量','参考取得単価','参考取得総額','参考売却益','原価確認','売却益確認','価格単位'],22)
    gain=defaultdict(Decimal);bad=defaultdict(bool)
    for x in trades:
        if x['side']!='売':continue
        k=(x['account'],instrument_id(x),trade_label(x,trades));g=detail.get(x['record_id'],{}).get('reference_gain')
        if g is None:bad[k]=True
        else:gain[k]+=g
    labels={a['id']:a['label'] for a in store.accounts()}
    for r,(k,s) in enumerate(sorted(states.items()),2):
        t.row(r,[labels[k[0]],code(reporting_code(s)),s['name'],s['custody'],s['quantity'],display_average(s),s['cost'] if s['valid'] else None,None if bad[k] or not s['valid'] else gain[k],0 if s['valid'] else 1,1 if bad[k] or not s['valid'] else 0,10000 if s.get('asset_type')=='投資信託' else 1],22)
        t.put(r,33,s['reason'] or '登録済み履歴による参考値')
    t.row(1,['日付','口座','コード','預り','売買','数量','売却原価','売却益','取引後株数','取引後原価','状態','受渡日','買付約定額','受渡日未登録の買付'],35)
    for r,x in enumerate(sorted(trades,key=lambda a:a['trade_date'],reverse=True),2):
        z=detail.get(x['record_id'],{})
        t.row(r,[x['trade_date'],x['account_label'],code(reporting_code(x)),trade_label(x,trades),x['side'],number(x['quantity']),z.get('sale_cost'),z.get('reference_gain'),z.get('quantity_after'),z.get('cost_after'),z.get('status'),x.get('settlement_date') or None,number(Decimal(x['settlement_amount'])-Decimal(x['fees'])-Decimal(x['tax'])) if x['side']=='買' else 0,number(Decimal(x['settlement_amount'])-Decimal(x['fees'])-Decimal(x['tax'])) if x['side']=='買' and not x.get('settlement_date') else 0],35)
    t.row(23,['I列＝実際の受渡金額。買付約定額＝I−J、売却約定額＝I＋J。'],12)
    t.row(24,['単価表示は小数0桁。元の小数値は保持。分割の数量は追加株数。'],12)
    t.row(26,['F-から始まるコードは、コードのない投資信託を区別する内部識別子。証券コードではありません。'],12)
    t.row(25,['右側の参考原価は移動平均。履歴不足・同日売買順不明は金額を空欄。'],12)
    from asset_snapshots import snapshots
    snap=snapshots(store)
    for base,dt in ((1,prev.isoformat()),(11,start.isoformat())):
        t.row(base,[dt,'国内株式等','外国株式(円)','現金(円)','外貨現金(円)','合計(円)','成長枠残高','積立枠残高','確認状態'],12)
        for r,a in enumerate(store.accounts(),base+1):
            s=snap.get((a['id'],dt));t.put(r,12,a['label'])
            for c,key in enumerate(('domestic','foreign','cash','foreign_cash'),13):t.put(r,c,None if a['id']=='E' or not s else s.get(key),'number')
            t.formula(r,17,f'IF(COUNT(M{r}:P{r})=4,SUM(M{r}:P{r}),"")')
            t.put(r,18,None if a['id']=='E' or not s else s.get('growth_remaining'),'number')
            t.put(r,19,None if a['id']=='E' or not s else s.get('tsumitate_remaining'),'number')
            t.put(r,20,'父は手動' if a['id']=='E' else (s.get('note','') if s else '未取得'))
    t.merges=[f'L{r}:R{r}' for r in (21,23,24,25,26)]
    t.row(21,['日付は実際の評価基準日。外貨はSBI表示の円評価を保存。買付余力は合計しない。'],12)
    # Public research data, date-specific and never silently reused next year.
    from market_data import load_research
    research=load_research(store.directory,year)
    facts=research.get('securities',{})
    d.row(1,['コード','銘柄','権利付き最終日','配当金支払日','本決算発表日','株数','DPS(税引前)','配当利回り','実績(税引後)','予想(税引後参考)','年間予想','累計配当金'],20)
    d.row(1,['確認状態','前月最終終値','株価日','出典・確認日'],33)
    def actual(c):return sum(number(e.get('net',e.get('amount'))) or 0 for e in events if e['type']=='配当' and str(e.get('code') or '未分類')==c and e['date']>=f'{year}-01-01')
    def cum(c):return sum(number(e.get('net',e.get('amount'))) or 0 for e in events if e['type']=='配当' and str(e.get('code') or '未分類')==c and e['date']>='2024-01-01')
    from dividend_forecast import forecast
    predicted,forecast_lines=forecast(store,activities,facts,day)
    forecast_start=max(40,len(names)+6)
    d.row(forecast_start-1,['コード','口座','預り','権利最終日','支払日','株数基準日','DPS','税引後係数','権利株数','予想受取額','確認状態'],33)
    for r,e in enumerate(forecast_lines,forecast_start):
        d.row(r,[code(e['code']),labels[e['account']],e['custody']],33)
        for c,key in ((36,'rights'),(37,'payment'),(38,'quantity_date')):d.put(r,c,e[key],'date')
        d.row(r,[e['dps'],e['net_rate']],39)
        d.formula(r,41,qty_formula(f'AG{r}',f'AH{r}',f'AL{r}',f'AI{r}'))
        d.formula(r,42,f'AO{r}*AM{r}*AN{r}')
        d.put(r,43,'将来権利：現在株数で仮計算' if e['estimated'] else '登録履歴から計算')
    # Ordering uses known actual + calculable forecast; unknown future is explicitly flagged.
    ordered=sorted(names,key=lambda c:(-(actual(c)+(predicted.get(c) or 0)),-cum(c),c))
    for r,c in enumerate(ordered,2):
        f=facts.get(c,{})
        d.row(r,[code(c),names[c],f.get('rights','未確認'),f.get('payments','未確認'),f.get('earnings','未確認')],20)
        q=qty_formula(f'T{r}');d.formula(r,25,f'IF(OR({q}<0,SUMIFS(取引!$AD$2:$AD$10000,取引!$W$2:$W$10000,T{r})>0),"",{q})')
        d.put(r,26,f.get('dps'),'number')
        price=f.get('close') if f.get('close_date','')[:7]==prev.isoformat()[:7] else None
        d.formula(r,27,f'IF(OR(NOT(ISNUMBER(Z{r})),NOT(ISNUMBER(AH{r})),AH{r}=0),"",Z{r}/AH{r})','percent')
        d.formula(r,28,cash_formula(f'T{r}',year))
        # No arbitrary conversion of current holdings into historical entitlement.
        future=predicted.get(c)
        if future is None:d.put(r,29,None,'number')
        else:d.formula(r,29,f'SUMIFS($AP${forecast_start}:$AP$10000,$AG${forecast_start}:$AG$10000,T{r})')
        d.formula(r,30,f'IF(ISNUMBER(AC{r}),AB{r}+AC{r},"")')
        d.formula(r,31,cash_formula(f'T{r}'))
        d.row(r,[f.get('status','今年の予定・権利株数が未確認。予想は空欄。'),price,f.get('close_date') if price is not None else '',f.get('source','')],33)
    summary_row=max(3,len(ordered)+2)
    d.put(summary_row,21,'合計')
    for c in (28,29,30,31):
        letter=col(c)
        if c in (29,30):d.formula(summary_row,c,f'IF(COUNT({letter}2:{letter}{max(2,len(ordered)+1)})={len(ordered)},SUM({letter}2:{letter}{max(2,len(ordered)+1)}),"")')
        else:d.formula(summary_row,c,f'SUM({letter}2:{letter}{max(2,len(ordered)+1)})')
    annual=max(len(ordered)+5,17)
    d.row(annual,['年','配当(税引後)','税還付','利息'],21)
    for r,y in enumerate(range(2024,year+1),annual+1):
        d.put(r,21,y)
        for c,kind in ((22,'配当'),(23,'税還付'),(24,'利息')):
            d.formula(r,c,f'SUMIFS($I$2:$I$10000,$F$2:$F$10000,"{kind}",$A$2:$A$10000,">="&DATE(U{r},1,1),$A$2:$A$10000,"<"&DATE(U{r}+1,1,1))')
    total=annual+year-2024+2
    d.put(total,21,'累計');d.formula(total,22,f'SUM(V{annual+1}:V{total-1})')
    d.put(total+1,21,'銘柄別との差');d.formula(total+1,22,f'V{total}-SUM(AE2:AE{max(2,len(ordered)+1)})')
    d.row(total+3,['実績・累計は税引後入金。DPS・利回りは税引前。未知の将来額は0にしない。'],21)
    d.row(total+4,['税還付・利息は銘柄別配当に含めない。株数は登録履歴のみ。'],21)
    for sh in (t,d):
        for c in range(12,49):sh.widths[str(c)]=18 if c not in (21,24,33,36,45) else 36
    d.widths['21']=42
    sheets=[t,d]
    if management:
        m=Sheet('管理見本');m.widths={str(i):18 for i in range(1,22)};m.widths['1']=45;m.widths['11']=12;m.widths['12']=35
        m.merges=[f'A{r}:J{r}' for r in range(1,6)]
        m.row(1,['管理見本 — 灰色のコード・株価は手入力。取引と配当は再出力後に同名シートへ差し替え。'])
        m.row(2,['実資料の一部だけを読み込んだ見本です。空欄は未取得・履歴不足であり、0円ではありません。'])
        m.row(3,['取得単価・売却益は右側の参考原価表から取得。売却益＋2024年以降の配当＝実現収益(参考)。'])
        m.row(4,['父の資産集計は手入力。円グラフは正の評価額が入力された銘柄だけ表示。'])
        m.row(50,['銘柄','コード','株数','株価(手入力)','取得単価(参考)','評価額','含み損益','損益率','実現収益(参考)','合計損益','コード','銘柄辞書'])
        for r,c in enumerate(ordered,51):
            m.put(r,2,code(c),fill='E7E6E6');m.put(r,4,None,'number','E7E6E6');m.put(r,11,code(c));m.put(r,12,names[c])
            m.formula(r,1,f'IFERROR(VLOOKUP(B{r},$K$51:$L${50+len(ordered)},2,FALSE),"")','text')
            q=qty_formula(f'B{r}');m.formula(r,3,f'IF(OR({q}<0,SUMIFS(取引!$AD$2:$AD$10000,取引!$W$2:$W$10000,B{r})>0),"",{q})')
            m.formula(r,5,f'IF(OR(NOT(ISNUMBER(C{r})),C{r}<=0,SUMIFS(取引!$AD$2:$AD$10000,取引!$W$2:$W$10000,B{r})>0),"",SUMIFS(取引!$AB$2:$AB$10000,取引!$W$2:$W$10000,B{r})/C{r}*M{r})')
            m.formula(r,6,f'IF(AND(ISNUMBER(C{r}),C{r}=0),0,IF(OR(NOT(ISNUMBER(C{r})),NOT(ISNUMBER(D{r}))),"",C{r}*D{r}/M{r}))')
            m.formula(r,7,f'IF(AND(ISNUMBER(C{r}),C{r}=0),0,IF(OR(NOT(ISNUMBER(F{r})),NOT(ISNUMBER(E{r}))),"",F{r}-C{r}*E{r}/M{r}))')
            m.formula(r,8,f'IF(OR(NOT(ISNUMBER(G{r})),NOT(ISNUMBER(E{r})),E{r}=0,C{r}=0),"",G{r}/(C{r}*E{r}/M{r}))','percent')
            m.formula(r,9,f'IF(SUMIFS(取引!$AE$2:$AE$10000,取引!$W$2:$W$10000,B{r})>0,"",SUMIFS(取引!$AC$2:$AC$10000,取引!$W$2:$W$10000,B{r})+{cash_formula(f"B{r}")})')
            m.formula(r,10,f'IF(OR(NOT(ISNUMBER(G{r})),NOT(ISNUMBER(I{r}))),"",G{r}+I{r})')
            m.put(50,13,'価格単位（口数）')
            m.put(r,13,10000 if any(reporting_code(st)==c and st.get('asset_type')=='投資信託' for st in states.values()) else 1,'code')
            m.formula(r,14,f'A{r}','text');m.formula(r,15,f'IF(F{r}>0,F{r},0)')
        summary=51+len(ordered)
        m.put(summary,1,'現金（手入力）');m.put(summary,6,None,'number','E7E6E6')
        m.put(summary+1,1,'合計（入力済み分）')
        for c in (6,7,9,10):m.formula(summary+1,c,f'SUM({col(c)}51:{col(c)}{summary})')
        accounts_row=max(60,summary+4)
        m.row(accounts_row,['口座','銘柄','コード','預り','保有数量','評価額','含み損益','状態','平均取得単価','取得総額'])
        rr=accounts_row+1
        for idx,(key,state) in enumerate(sorted(states.items()),2):
            ident=reporting_code(state);account=labels[key[0]]
            m.row(rr,[account,state['name'],code(ident),state['custody']])
            m.put(rr,3,code(ident),'code')
            if key[0]=='E':
                for c in (5,6,7,9,10):m.put(rr,c,None,'number','E7E6E6')
                m.put(rr,8,'父：手入力')
            else:
                q=qty_formula(f'C{rr}',f'A{rr}',custody_ref=f'D{rr}')
                m.formula(rr,5,f'IF({q}<0,"",{q})')
                criteria=f'取引!$V$2:$V$10000,A{rr},取引!$W$2:$W$10000,C{rr},取引!$Y$2:$Y$10000,D{rr}'
                known=f'AND(COUNTIFS({criteria})=1,SUMIFS(取引!$AD$2:$AD$10000,{criteria})=0)'
                m.formula(rr,9,f'IF({known},SUMIFS(取引!$AA$2:$AA$10000,{criteria}),"")')
                m.formula(rr,10,f'IF({known},SUMIFS(取引!$AB$2:$AB$10000,{criteria}),"")')
                m.formula(rr,6,f'IFERROR(IF(COUNTIF($B$51:$B${summary-1},C{rr})=0,"",IF(ISNUMBER(INDEX($D$51:$D${summary-1},MATCH(C{rr},$B$51:$B${summary-1},0))),E{rr}*INDEX($D$51:$D${summary-1},MATCH(C{rr},$B$51:$B${summary-1},0))/SUMIFS(取引!$AF$2:$AF$10000,{criteria}),"")),"")')
                m.formula(rr,7,f'IF(AND(ISNUMBER(F{rr}),ISNUMBER(J{rr})),F{rr}-J{rr},"")')
                m.formula(rr,8,f'IF({known},"参考","原価要確認")','text')
            rr+=1
        nr=max(71,rr+2)
        m.row(nr,['使用枠（受渡年）']+[a['label'] for a in store.accounts()])
        m.put(nr+1,1,'対象年');m.put(nr+1,2,year,'code','E7E6E6')
        for offset,bucket in enumerate(('NISA(成)','NISA(積)','NISA(要確認)'),2):
            m.put(nr+offset,1,bucket)
            for c,a in enumerate(store.accounts(),2):
                m.formula(nr+offset,c,f'SUMIFS(取引!$AU$2:$AU$10000,取引!$AJ$2:$AJ$10000,{col(c)}${nr},取引!$AL$2:$AL$10000,$A{nr+offset},取引!$AT$2:$AT$10000,">="&DATE($B${nr+1},1,1),取引!$AT$2:$AT$10000,"<"&DATE($B${nr+1}+1,1,1))')
        m.put(nr+5,1,'受渡日未登録の買付')
        for c in range(2,7):m.formula(nr+5,c,f'SUMIFS(取引!$AV$2:$AV$10000,取引!$AJ$2:$AJ$10000,{col(c)}${nr})')
        m.put(nr+6,1,'上の未登録額がある場合、枠使用額は未確定。売却分を差し引きません。')
        assetrow=nr+8
        for offset,sourcebase,dt in ((0,2,prev.isoformat()),(9,12,start.isoformat())):
            ar=assetrow+offset
            m.row(ar,[dt,'国内株式等','外国株式(円)','現金(円)','外貨現金(円)','運用資産計'])
            for ix,a in enumerate(store.accounts()):
                rr=ar+1+ix;m.put(rr,1,a['label'])
                if a['id']=='E':
                    for c in range(2,7):m.put(rr,c,None,'number','E7E6E6')
                else:
                    for c,src in enumerate(('M','N','O','P'),2):m.formula(rr,c,f'IF(ISNUMBER(取引!{src}{sourcebase+ix}),取引!{src}{sourcebase+ix},"")')
                    m.formula(rr,6,f'IF(COUNT(B{rr}:E{rr})=4,SUM(B{rr}:E{rr}),"")')
            m.put(ar+6,1,'父の資産額は灰色へ手入力。未取得は空欄。証券の買付余力は加算しない。')
        m.chart={'labels':f'N51:N{50+len(ordered)}','values':f'O51:O{50+len(ordered)}','title':'株式ポートフォリオ（評価額入力後）','anchor':'N62:W79'} if ordered else None
        sheets.append(m)
    for sh in sheets:
        codecols={'取引':{4,23,37},'配当':{4,20},'管理見本':{2,11}}[sh.name]
        for cell in sh.cells:
            if sh.name=='取引' and cell['v'] and ((cell['c'] in (35,46) and cell['r']>1) or (cell['c']==12 and cell['r'] in (1,11))):cell['k']='date'
            if cell['c'] in codecols and cell['k']=='number':cell['k']='code'
    return {'version':'3.3.0','as_of':day,'sheets':[s.result() for s in sheets]}
