"""Persist dividends and splits separately from trades; keep balance reports as evidence."""
import hashlib
import json
import re
import sqlite3
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


def init_notice_tables(db):
    """Create verified notice evidence inside the caller's transaction.

    A legacy activity_files row proves only that one event was registered.
    It never creates a completion record by itself. Do not use executescript:
    its implicit commit would break the enclosing financial transaction.
    """
    db.execute('''CREATE TABLE IF NOT EXISTS activity_documents(
      account TEXT NOT NULL,digest TEXT NOT NULL,parser_revision INTEGER NOT NULL,
      item_count INTEGER NOT NULL,manifest TEXT NOT NULL,
      PRIMARY KEY(account,digest),
      FOREIGN KEY(account,digest) REFERENCES activity_files(account,digest))''')
    db.execute('''CREATE TABLE IF NOT EXISTS activity_file_events(
      account TEXT NOT NULL,digest TEXT NOT NULL,activity_id INTEGER NOT NULL,
      PRIMARY KEY(account,digest,activity_id),
      FOREIGN KEY(account,digest) REFERENCES activity_documents(account,digest),
      FOREIGN KEY(activity_id) REFERENCES activities(id))''')


def _canonical_payload(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,
                      separators=(',',':'),allow_nan=False)


def _strict_json(value):
    def unique(pairs):
        result={}
        for name,item in pairs:
            if name in result:raise ValueError('Duplicate JSON key')
            result[name]=item
        return result
    return json.loads(value,object_pairs_hook=unique)


def _payload_digest(value):
    return hashlib.sha256(_canonical_payload(value).encode('utf-8')).hexdigest()


def _notice_revision():
    from corporate_actions import NOTICE_PARSER_REVISION
    return NOTICE_PARSER_REVISION


def _notice_items(document):
    # Older callers and single-item split/tax documents have no populated items.
    items=getattr(document,'items',None)
    if items is None or items==():items=(document.item,)
    if not isinstance(items,(list,tuple)) or not items:
        raise LedgerError('通知に有効な明細がありません。原本を確認してください。')
    result=[];seen=set()
    for event in items:
        if not isinstance(event,dict) or event.get('type') not in ('配当','株式分割','税還付','利息'):
            raise LedgerError('通知の明細区分を確認できません。原本を確認してください。')
        if not isinstance(event.get('date'),str) or parse_date(event['date'])!=event['date']:
            raise LedgerError('通知の日付を確認できません。原本を確認してください。')
        try:
            event_key=key(event);_canonical_payload(event)
        except (TypeError,ValueError,OverflowError):
            raise LedgerError('通知の明細を確認できません。原本を確認してください。') from None
        if event_key in seen:
            raise LedgerError('同じ通知内に重複または区別できない明細があります。原本を確認してください。')
        seen.add(event_key);result.append(dict(event))
    return tuple(result)


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

    def _notice_tables_exist(self):
        return self.db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('activity_documents','activity_file_events')").fetchone()[0]==2

    def notice_is_complete(self,account,digest):
        """Read-only proof that this parser revision registered every event."""
        if not self._notice_tables_exist():return False
        try:
            document=self.db.execute('''SELECT parser_revision,item_count,manifest
                FROM activity_documents WHERE account=? AND digest=?''',(account,digest)).fetchone()
            if document is None or document['parser_revision']!=_notice_revision():return False
            count=document['item_count'];raw=document['manifest']
            if type(count) is not int or not 1<=count<=10000 or not isinstance(raw,str) or len(raw)>4*1024*1024:return False
            manifest=_strict_json(raw)
            if not isinstance(manifest,list) or len(manifest)!=count:return False
            expected={}
            for item in manifest:
                if not isinstance(item,dict) or set(item)!={'event_key','payload_sha256'}:return False
                if any(not isinstance(item[name],str) or not re.fullmatch(r'[0-9a-f]{64}',item[name]) for name in item):return False
                if item['event_key'] in expected:return False
                expected[item['event_key']]=item['payload_sha256']
            anchor=self.db.execute('SELECT activity_id FROM activity_files WHERE account=? AND digest=?',(account,digest)).fetchone()
            if anchor is None:return False
            rows=list(self.db.execute('''SELECT l.activity_id,a.account,a.kind,a.event_date,a.event_key,a.payload
                FROM activity_file_events l LEFT JOIN activities a ON a.id=l.activity_id
                WHERE l.account=? AND l.digest=?''',(account,digest)))
            if len(rows)!=count or anchor['activity_id'] not in {row['activity_id'] for row in rows}:return False
            found={}
            for row in rows:
                if row['account']!=account:return False
                payload=_strict_json(row['payload'])
                if not isinstance(payload,dict) or payload.get('type')!=row['kind'] or payload.get('date')!=row['event_date']:return False
                if key(payload)!=row['event_key'] or row['event_key'] in found:return False
                found[row['event_key']]=_payload_digest(payload)
            return found==expected
        except (sqlite3.DatabaseError,KeyError,TypeError,ValueError,OverflowError,RecursionError):
            return False

    def completed_notice_digests(self,account):
        """Return only verified notices; merely exporting never migrates data."""
        if not self._notice_tables_exist():return set()
        try:
            digests=[row[0] for row in self.db.execute('SELECT digest FROM activity_documents WHERE account=?',(account,))]
        except sqlite3.DatabaseError:return set()
        return {digest for digest in digests if self.notice_is_complete(account,digest)}

    def _preview_notice_item(self,account,event,peer_payloads):
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
            if event['type']=='株式分割' and other.get('code')==event.get('code') and other.get('manual'):
                same=all(Decimal(str(other[k]))==Decimal(str(event[k])) for k in ('before','added','after'))
                same=same and Decimal(str(other['ratio_new']))/Decimal(str(other['ratio_old']))==Decimal(str(event['ratio_new']))/Decimal(str(event['ratio_old']))
                if same:
                    matching_id=row['id'];continue
            if (other.get('code')==event.get('code') and other.get('custody','')==event.get('custody','')
                    and other!=event and _canonical_payload(other) not in peer_payloads):
                raise LedgerError('同じ口座・銘柄・日付の通知が保存済みの内容と異なります。原本を確認してください。')
        existing=self.db.execute('SELECT id,payload FROM activities WHERE account=? AND event_key=?',(account,key(event))).fetchone()
        if existing and json.loads(existing['payload'])!=event:
            raise LedgerError('同じ銘柄・日付の通知が保存済みの内容と食い違います。登録を止めました。')
        ident=existing['id'] if existing else matching_id
        return {'same_event':ident is not None,'matching_id':matching_id,'activity_id':ident,
                'event_key':key(event),'new':int(ident is None),'existing':int(ident is not None)}

    def preview_notice(self,account,document):
        """Validate every item without touching rows, schema or raw evidence."""
        self._owner(account,document.owner)
        items=_notice_items(document);peers={_canonical_payload(event) for event in items}
        outcomes=tuple(self._preview_notice_item(account,event,peers) for event in items)
        identifiers=[item['activity_id'] for item in outcomes if item['activity_id'] is not None]
        if len(identifiers)!=len(set(identifiers)):
            raise LedgerError('通知の複数明細が同じ保存済み入金に対応します。原本を確認してください。')
        anchor=self.db.execute('SELECT activity_id FROM activity_files WHERE account=? AND digest=?',(account,document.digest)).fetchone()
        if anchor is not None and anchor['activity_id'] not in identifiers:
            raise LedgerError('保存済み通知の対応先と今回の明細が一致しません。既存記録を保持して停止しました。')
        linked=set()
        if self._notice_tables_exist():
            linked={row[0] for row in self.db.execute('SELECT activity_id FROM activity_file_events WHERE account=? AND digest=?',(account,document.digest))}
            if not linked<=set(identifiers):
                raise LedgerError('保存済み通知の明細対応を確認できません。既存記録を保持して停止しました。')
            previous=self.db.execute('SELECT parser_revision,item_count FROM activity_documents WHERE account=? AND digest=?',(account,document.digest)).fetchone()
            if previous is not None and (previous['parser_revision']>_notice_revision() or previous['item_count']>len(items)):
                raise LedgerError('保存済み通知より少ない明細・古い解析結果では更新できません。既存記録を保持しました。')
        already=(self.notice_is_complete(account,document.digest)
                 and len(identifiers)==len(items) and linked==set(identifiers))
        return {'already':already,'same_event':all(item['same_event'] for item in outcomes),
                'matching_id':outcomes[0]['matching_id'] if len(outcomes)==1 else None,
                'new':sum(item['new'] for item in outcomes),'existing':sum(item['existing'] for item in outcomes),
                'items':outcomes}

    def import_notice(self,account,document):
        preview=self.preview_notice(account,document)
        if preview['already']:return preview
        if not isinstance(document.raw,bytes) or hashlib.sha256(document.raw).hexdigest()!=document.digest:
            raise LedgerError('通知の原本と確認済みの内容が一致しません。最初から確認してください。')
        self.store.backup()
        archive=self.store.directory/'originals'/account/f'{document.digest}.pdf'
        archive.parent.mkdir(parents=True,exist_ok=True)
        if archive.exists():
            if hashlib.sha256(archive.read_bytes()).hexdigest()!=document.digest:
                raise LedgerError('保存済み通知の原本が一致しません。上書きせず停止しました。')
        else:archive.write_bytes(document.raw)
        # SAVEPOINT also makes schema DDL transactional, including on a legacy DB.
        self.db.execute('SAVEPOINT notice_import')
        try:
            preview=self.preview_notice(account,document)
            if preview['already']:
                self.db.execute('RELEASE SAVEPOINT notice_import');return preview
            init_notice_tables(self.db)
            identifiers=[]
            for event,outcome in zip(_notice_items(document),preview['items']):
                ident=outcome['activity_id']
                if ident is None:
                    ident=self.db.execute('INSERT INTO activities(account,kind,event_key,event_date,payload,source) VALUES (?,?,?,?,?,?)',
                        (account,event['type'],key(event),event['date'],json.dumps(event,ensure_ascii=False),document.filename)).lastrowid
                else:
                    self.db.execute('UPDATE activities SET source=? WHERE id=?',(document.filename,ident))
                identifiers.append(ident)
            anchor=self.db.execute('SELECT activity_id FROM activity_files WHERE account=? AND digest=?',(account,document.digest)).fetchone()
            if anchor is None:
                self.db.execute('INSERT INTO activity_files(account,digest,activity_id) VALUES (?,?,?)',(account,document.digest,identifiers[0]))
            elif anchor['activity_id'] not in identifiers:
                raise LedgerError('保存済み通知の対応先と明細が一致しません。')
            manifest=[]
            for ident in identifiers:
                row=self.db.execute('SELECT event_key,payload FROM activities WHERE id=? AND account=?',(ident,account)).fetchone()
                if row is None:raise LedgerError('通知の口座・明細の対応を確認できません。')
                manifest.append({'event_key':row['event_key'],'payload_sha256':_payload_digest(_strict_json(row['payload']))})
            self.db.execute('''INSERT INTO activity_documents(account,digest,parser_revision,item_count,manifest)
                VALUES (?,?,?,?,?) ON CONFLICT(account,digest) DO UPDATE SET
                parser_revision=excluded.parser_revision,item_count=excluded.item_count,manifest=excluded.manifest''',
                (account,document.digest,_notice_revision(),len(identifiers),_canonical_payload(manifest)))
            for ident in identifiers:
                self.db.execute('INSERT OR IGNORE INTO activity_file_events(account,digest,activity_id) VALUES (?,?,?)',(account,document.digest,ident))
            if not self.notice_is_complete(account,document.digest):
                raise LedgerError('通知の全明細が保存されたことを確認できません。登録前へ戻します。')
            self.db.execute('RELEASE SAVEPOINT notice_import')
        except BaseException:
            self.db.execute('ROLLBACK TO SAVEPOINT notice_import')
            self.db.execute('RELEASE SAVEPOINT notice_import')
            raise
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
