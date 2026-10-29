"""Persist dividends and splits separately from trades; keep balance reports as evidence."""
import hashlib
import json
import re
import unicodedata
import calendar
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from ledger import LedgerError,parse_date

def key(event):
    # One split per security/effective day; a dividend may have several payments.
    fields=['type','date','code','name']
    if event['type']=='配当':fields+=['quantity','gross','net','custody','currency']
    elif event['type'] in ('税還付','利息'):fields+=['amount','description','currency']
    elif event['type']=='株式分割':fields+=['ratio_old','ratio_new','before','added','after']
    return hashlib.sha256(json.dumps([str(event.get(f,'')) for f in fields],ensure_ascii=False).encode()).hexdigest()

def normalized_name(s):
    return re.sub(r'\s+','',unicodedata.normalize('NFKC',s))

class Activities:
    def __init__(self,store):
        self.store=store;self.db=store.db
        with self.db:
            self.db.executescript('''
            CREATE TABLE IF NOT EXISTS activities(
              id INTEGER PRIMARY KEY,account TEXT NOT NULL REFERENCES accounts(id),
              kind TEXT NOT NULL,event_key TEXT NOT NULL,event_date TEXT NOT NULL,
              payload TEXT NOT NULL,source TEXT NOT NULL DEFAULT '',
              UNIQUE(account,event_key));
            CREATE TABLE IF NOT EXISTS activity_files(
              id INTEGER PRIMARY KEY,account TEXT NOT NULL REFERENCES accounts(id),
              digest TEXT NOT NULL,activity_id INTEGER NOT NULL REFERENCES activities(id),
              UNIQUE(account,digest));
            CREATE TABLE IF NOT EXISTS balance_reports(
              id INTEGER PRIMARY KEY,account TEXT NOT NULL REFERENCES accounts(id),
              digest TEXT NOT NULL,as_of TEXT NOT NULL,payload TEXT NOT NULL,
              filename TEXT NOT NULL,UNIQUE(account,digest));
            ''')

    def _valid_account(self,account):
        if not self.db.execute('SELECT 1 FROM accounts WHERE id=?',(account,)).fetchone():
            raise LedgerError('口座を選んでください。')

    def _owner(self,account,owner):
        row=self.db.execute('SELECT label,owner_name FROM accounts WHERE id=?',(account,)).fetchone()
        if row is None:raise LedgerError('口座を選んでください。')
        expected=row['owner_name'] or row['label']
        if expected in ('母','父'):
            raise LedgerError('この口座では先に「PDF名義を設定」で報告書の氏名を登録してください。')
        if normalized_name(expected) not in normalized_name(owner):
            raise LedgerError(f'通知の名義「{owner}」と口座名「{expected}」が一致しません。口座を確認してください。')

    def preview_notice(self,account,document):
        self._owner(account,document.owner)
        event=document.item
        file_existing=self.db.execute('SELECT 1 FROM activity_files WHERE account=? AND digest=?',(account,document.digest)).fetchone()
        matching_id=None
        for row in self.db.execute('SELECT id,payload FROM activities WHERE account=? AND kind=? AND event_date=?',
                                   (account,event['type'],event['date'])):
            other=json.loads(row['payload'])
            if (event['type'] in ('税還付','利息') and other.get('manual')
                    and not other.get('code') and not event.get('code')
                    and other.get('currency','JPY')==event.get('currency','JPY')=='JPY'):
                try:same=Decimal(str(other.get('amount',other.get('net'))))==Decimal(str(event.get('amount',event.get('net'))))
                except Exception:same=False
                if same:
                    if matching_id is not None and matching_id!=row['id']:
                        raise LedgerError('同じ口座・日付・金額の手動入金が複数あります。原本との対応を確認してください。')
                    matching_id=row['id'];continue
            if event['type']=='株式分割' and other['code']==event['code'] and other.get('manual'):
                same=all(Decimal(str(other[k]))==Decimal(str(event[k])) for k in ('before','added','after'))
                same=same and Decimal(str(other['ratio_new']))/Decimal(str(other['ratio_old']))==Decimal(str(event['ratio_new']))/Decimal(str(event['ratio_old']))
                if same:
                    matching_id=row['id'];continue
            if other['code']==event['code'] and other.get('custody','')==event.get('custody','') and other!=event:
                raise LedgerError('同じ口座・銘柄・日付の通知が保存済みの内容と異なります。原本を確認してください。')
        existing=self.db.execute('SELECT payload FROM activities WHERE account=? AND event_key=?',(account,key(event))).fetchone()
        if existing and json.loads(existing['payload'])!=event:
            raise LedgerError('同じ銘柄・日付の通知が保存済みの内容と食い違います。登録を止めました。')
        return {'already':bool(file_existing),'same_event':bool(existing) or matching_id is not None,'matching_id':matching_id}

    def import_notice(self,account,document):
        preview=self.preview_notice(account,document)
        if preview['already']:return preview
        self.store.backup()
        archive=self.store.directory/'originals'/account/f'{document.digest}.pdf'
        archive.parent.mkdir(parents=True,exist_ok=True)
        if not archive.exists():archive.write_bytes(document.raw)
        with self.db:
            existing=self.db.execute('SELECT id FROM activities WHERE account=? AND event_key=?',(account,key(document.item))).fetchone()
            if existing or preview.get('matching_id'):
                ident=existing['id'] if existing else preview['matching_id']
                self.db.execute('UPDATE activities SET source=? WHERE id=?',(document.filename,ident))
            else:
                ident=self.db.execute('INSERT INTO activities(account,kind,event_key,event_date,payload,source) VALUES (?,?,?,?,?,?)',
                    (account,document.item['type'],key(document.item),document.item['date'],json.dumps(document.item,ensure_ascii=False),document.filename)).lastrowid
            self.db.execute('INSERT INTO activity_files(account,digest,activity_id) VALUES (?,?,?)',(account,document.digest,ident))
        return preview

    def add_cash(self,account,kind,event_date,amount,description='',code='',custody='',tax='0',quantity='',per_share=''):
        """Add a user-confirmed cash item that has no supported PDF layout yet."""
        self._valid_account(account)
        if kind not in ('配当','税還付','利息'):
            raise LedgerError('手動の入金区分は、配当・税還付・利息から選んでください。')
        event_date=parse_date(event_date)
        try:value=Decimal(str(amount).replace(',','').strip())
        except Exception as error:raise LedgerError('金額を数値で入力してください。') from error
        if not value.is_finite() or value<=0:raise LedgerError('金額は0より大きい円額を入力してください。')
        description=description.strip() or kind
        if len(description)>100:raise LedgerError('内容は100文字以内で入力してください。')
        normalized=format(value,'f').rstrip('0').rstrip('.') if '.' in format(value,'f') else format(value,'f')
        event={'type':kind,'date':event_date,'name':description,'code':'','amount':normalized,
               'gross':normalized,'tax':'0','net':normalized,'description':description,
               'currency':'JPY','manual':True}
        code=str(code).strip().upper()
        if code and not re.fullmatch(r'[0-9A-Z]{4}',code):raise LedgerError('コードは4文字の数字・英字で入力してください。')
        def numeric(v,empty=False):
            if empty and str(v).strip()=='':return ''
            try:d=Decimal(str(v).replace(',',''))
            except Exception:raise LedgerError('税・株数・単価は数値で入力してください。')
            if not d.is_finite() or d<0:raise LedgerError('税・株数・単価は0以上の数値です。')
            return format(d,'f')
        tax=numeric(tax);qty=numeric(quantity,True);dps=numeric(per_share,True)
        if kind=='配当':
            event.update(code=code,custody=custody.strip(),tax=tax,gross=format(value+Decimal(tax),'f'),quantity=qty,per_share=dps)
            if qty and dps and abs(Decimal(qty)*Decimal(dps)-Decimal(event['gross']))>Decimal('1'):
                raise LedgerError('株数×一株配当と税込配当が一致しません。')
        event_key=key(event)
        existing=self.db.execute('SELECT 1 FROM activities WHERE account=? AND event_key=?',(account,event_key)).fetchone()
        if existing:return False
        self.store.backup()
        with self.db:
            self.db.execute('INSERT INTO activities(account,kind,event_key,event_date,payload,source) VALUES (?,?,?,?,?,?)',
                (account,kind,event_key,event_date,json.dumps(event,ensure_ascii=False),'手動入力'))
        return True

    def import_balance(self,account,document):
        self._valid_account(account)
        if document.kind!='balance_pdf':raise LedgerError('取引残高報告書を選んでください。')
        self._owner(account,document.owner)
        existing=self.db.execute('SELECT 1 FROM balance_reports WHERE account=? AND digest=?',(account,document.digest)).fetchone()
        if existing:return False
        self.store.backup()
        archive=self.store.directory/'originals'/account/f'{document.digest}.pdf'
        archive.parent.mkdir(parents=True,exist_ok=True)
        if not archive.exists():archive.write_bytes(document.raw)
        with self.db:
            self.db.execute('INSERT INTO balance_reports(account,digest,as_of,payload,filename) VALUES (?,?,?,?,?)',
                (account,document.digest,document.item['as_of'],json.dumps(document.item,ensure_ascii=False),document.filename))
        return True

    def events(self,month=''):
        # Use the same date validation as the trade ledger.
        self.store.records(month)
        cutoff=month+'-31' if month else '9999-12-31'
        out=[]
        rows=self.db.execute('''SELECT v.*,a.label FROM activities v JOIN accounts a ON a.id=v.account
            WHERE event_date<=? ORDER BY event_date,account,id''',(cutoff,))
        for r in rows:
            d=json.loads(r['payload']);d.update(account=r['account'],account_label=r['label'],source=r['source'])
            out.append(d)
        return out

    def positions(self,month=''):
        self.store.records(month)
        if month:
            y,m=map(int,month.split('-'))
            cutoff=f'{month}-{calendar.monthrange(y,m)[1]:02}'
        else:cutoff='9999-12-31'
        return self.positions_as_of(cutoff)

    def positions_as_of(self,cutoff):
        parse_date(cutoff)
        trades=[t for t in self.store.records() if t['trade_date']<=cutoff]
        activities=[e for e in self.events() if e['date']<=cutoff]
        movements=[]
        for t in trades:
            if t.get('asset_type','国内株式')=='国内株式':
                movements.append((t['trade_date'],1,t['account'],t['code'],t['name'],t))
        for e in activities:
            if e['type']=='株式分割':movements.append((e['date'],0,e['account'],e['code'],e['name'],e))
        movements.sort(key=lambda v:(v[0],v[1]))
        def stock_count(value):
            try:
                number=Decimal(str(value))
                if isinstance(value,bool) or not number.is_finite() or number<0 or number!=number.to_integral_value():raise ValueError()
                return int(number)
            except Exception:raise LedgerError('保有株数・分割株数を確認できません。元の取引・分割記録を確認してください。') from None
        state=defaultdict(lambda:{'quantity':0,'warning':False,'name':''})
        for day,priority,account,code,name,record in movements:
            p=state[(account,code)];p['name']=name
            if priority==0:
                before=stock_count(record['before']);added=stock_count(record['added'])
                if p['quantity']!=before:p['warning']=True
                if 'after' in record and before+added!=stock_count(record['after']):p['warning']=True
                p['quantity']+=added
            else:
                p['quantity']+=stock_count(record['quantity'])*(1 if record['side']=='買' else -1)
                if p['quantity']<0:p['warning']=True
        accounts={a['id']:a['label'] for a in self.store.accounts()}
        return [dict(account=a,account_label=accounts[a],code=c,**v) for (a,c),v in sorted(state.items())]

    def balances(self):
        rows=self.db.execute('SELECT b.*,a.label FROM balance_reports b JOIN accounts a ON a.id=b.account ORDER BY as_of,account,id')
        return [dict(account=r['account'],account_label=r['label'],filename=r['filename'],**json.loads(r['payload'])) for r in rows]
