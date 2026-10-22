"""SMBC Nikko A501 cash-equity report, verified against the supplied two pages."""
import hashlib
import re
from decimal import Decimal
from ledger import LedgerError, Snapshot, decimal_text, parse_date, trade_key
from pdf_common import load_pdf, compact_text

def region(page, box):
    x0,y0,x1,y1=box
    words=sorted((w for w in page.words if x0 <= (w['x0']+w['x1'])/2 < x1 and y0 <= w['top'] < y1),key=lambda w:(round(w['top']/3),w['x0']))
    return compact_text(''.join(w['text'] for w in words))

def era_date(text):
    bits=re.findall(r'\d+',text)
    if len(bits)!=3:raise LedgerError('SMBCの年月日を確定できません。')
    y,m,d=map(int,bits)
    if y<100:y+=2018  # A501: Reiwa-era abbreviated date, not Gregorian two-digit year.
    if not 2019<=y<=2099:raise LedgerError('未対応のSMBC日付です。')
    return parse_date(f'{y}-{m:02}-{d:02}')

def parse_smbc(path):
    pdf=load_pdf(path,words=True,max_pages=50)
    trades=[];owners=set();ids=[]
    for page in pdf.pages:
        text=page.best_compact+page.compact
        if '取引報告書' not in text or 'A501' not in text or not 835<page.width<850 or not 590<page.height<605:
            raise LedgerError('SMBC日興証券A501の確認済み様式ではありません。')
        owner=region(page,(155,369,510,387))
        if not owner.endswith('様'):raise LedgerError('SMBCの名義を読み取れません。')
        owners.add(owner[:-1])
        trade_date=era_date(region(page,(180,150,245,170)))
        settlement=era_date('.'.join(region(page,(x,359,x+18,366)) for x in (542,566,590)))
        if settlement<trade_date:raise LedgerError('SMBCの受渡日が約定日より前です。')
        side=region(page,(14,198,33,215))
        if side not in ('買','売'):raise LedgerError('SMBCの現物売買以外は対象外です。')
        # This layout has room for more trades. Never silently ignore an extra row.
        if region(page,(14,234,830,263)):raise LedgerError('SMBCの1ページ複数明細は未検証です。')
        name=region(page,(33,198,490,215))
        code=region(page,(130,215,194,234)).strip('()')
        market=region(page,(194,215,212,234))
        if not name or not re.fullmatch(r'[0-9A-Z]{4,6}',code) or market!='東':
            raise LedgerError('SMBCの銘柄・市場を確認できません。')
        qty=decimal_text(region(page,(212,215,308,234)))
        price=decimal_text(region(page,(308,215,389,234)))
        gross=decimal_text(region(page,(389,215,485,234)))
        fee=decimal_text(region(page,(565,215,648,234)),missing_zero=True)
        tax=decimal_text(region(page,(565,198,648,215)),missing_zero=True)
        if region(page,(486,198,565,234)):raise LedgerError('SMBCの経過利息等がある取引は対象外です。')
        cash=Decimal(region(page,(728,215,833,234)).replace(',',''))
        q,p,g,f,t=map(Decimal,(qty,price,gross,fee,tax))
        if q<=0 or q*p!=g or cash!=(-g-f-t if side=='買' else g-f-t):
            raise LedgerError('SMBCの数量・単価・手数料・税・受渡金額が一致しません。')
        paid=region(page,(630,359,728,379));received=region(page,(730,359,832,379))
        check=paid if side=='買' else received
        if not check or Decimal(check)!=abs(cash):raise LedgerError('SMBCの支払・受取合計が一致しません。')
        custody=region(page,(648,198,728,234))
        if custody=='NISA':custody='NISA(成)' if side=='買' and trade_date>='2024-01-01' else 'NISA'
        elif '特定' in custody:custody='特定'
        elif '一般' in custody:custody='一般'
        else:raise LedgerError('SMBCの預り区分を確定できません。')
        row=dict(trade_date=trade_date,settlement_date=settlement,name=name,code=code,market='東証',side=side,
                 custody=custody,tax_class='非課税' if 'NISA' in custody else '課税',quantity=qty,price=price,
                 fees=fee,tax=tax,settlement_amount=decimal_text(abs(cash)),asset_type='国内株式',
                 quantity_unit='株',price_unit='円/株',currency='JPY',broker='SMBC日興証券')
        row['signature']=trade_key(row);trades.append(row)
        ids.append(region(page,(15,150,165,170))+trade_date+region(page,(104,390,140,409)))
    if len(owners)!=1:raise LedgerError('SMBCのページ間で名義が一致しません。')
    digest=hashlib.sha256(pdf.raw).hexdigest()
    return Snapshot(pdf.raw,pdf.path.name,digest,min(t['trade_date'] for t in trades),max(t['trade_date'] for t in trades),trades,
                    kind='pdf',owner=owners.pop(),document_key=hashlib.sha256(('SMBC:'+'|'.join(ids)).encode()).hexdigest())
