"""PDF ledger with legacy-import reconciliation. Python 3.10+, standard library only."""
from __future__ import annotations
import calendar
import hashlib
import html
import io
import json
import re
import shutil
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

HEADERS = ['約定日','銘柄','銘柄コード','市場','取引','期限','預り','課税',
           '約定数量','約定単価','手数料/諸経費等','税額','受渡日','受渡金額/決済損益']
FIELDS = ['trade_date','name','code','market','side','custody','tax_class',
          'quantity','price','fees','tax','settlement_date','settlement_amount']
LABELS = ['記録ID','口座ID','口座名','約定日','銘柄','コード','市場','売買','預り','課税',
          '数量','単価','手数料等','税額','受渡日','受渡金額','出典資料',
          '商品分類','数量単位','価格単位','通貨']
FAMILY_ACCOUNTS = {'A':'俊吾','B':'将太','C':'亮介','D':'母','E':'父'}

class LedgerError(ValueError):
    pass

def trade_key(t):
    keys=['trade_date','code','market','side','custody','quantity','price','fees','tax','settlement_date','settlement_amount']
    values=[]
    for key in keys:
        v=unicodedata.normalize('NFKC',str(t[key])).replace(' ','')
        if key=='market' and v=='東京':v='東証'
        values.append(v)
    if not str(t.get('code','')).strip():
        values.append(unicodedata.normalize('NFKC',str(t['name'])).replace(' ',''))
    category=t.get('asset_type','国内株式')
    if category!='国内株式':values.append(category)
    currency=t.get('currency','JPY')
    if currency!='JPY':values.append(currency)
    return hashlib.sha256(json.dumps(values,ensure_ascii=False).encode()).hexdigest()

def decimal_text(value, *, missing_zero=False):
    s=str(value).strip().replace(',','')
    if missing_zero and s in ('','--','－','-'): return '0'
    try: d=Decimal(s)
    except InvalidOperation as e: raise LedgerError(f'数値を読み取れません: {value}') from e
    if not d.is_finite() or d < 0: raise LedgerError(f'非負の数値が必要です: {value}')
    return format(d,'f').rstrip('0').rstrip('.') if '.' in format(d,'f') else format(d,'f')

def parse_date(s):
    s=s.strip()
    for fmt in ('%Y/%m/%d','%Y-%m-%d','%Y年%m月%d日'):
        try: return datetime.strptime(s,fmt).date().isoformat()
        except ValueError: pass
    raise LedgerError(f'日付を読み取れません: {s}')

@dataclass
class Snapshot:
    raw: bytes
    filename: str
    digest: str
    start: str
    end: str
    trades: list
    kind: str = 'csv'
    owner: str = ''
    document_key: str = ''


class Store:
    def __init__(self,directory):
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.directory/'ledger.sqlite3')
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY,label TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS imports(id INTEGER PRIMARY KEY,account TEXT NOT NULL REFERENCES accounts(id),
          digest TEXT NOT NULL,filename TEXT NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL,
          imported_at TEXT NOT NULL,UNIQUE(account,digest));
        CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY,account TEXT NOT NULL REFERENCES accounts(id),
          signature TEXT NOT NULL,occurrence INTEGER NOT NULL,payload TEXT NOT NULL,
          trade_date TEXT NOT NULL,import_id INTEGER NOT NULL REFERENCES imports(id),
          UNIQUE(account,signature,occurrence));
        ''')
        columns={r[1] for r in self.db.execute('PRAGMA table_info(imports)')}
        if 'kind' not in columns:self.db.execute("ALTER TABLE imports ADD COLUMN kind TEXT NOT NULL DEFAULT 'csv'")
        if 'document_key' not in columns:self.db.execute("ALTER TABLE imports ADD COLUMN document_key TEXT NOT NULL DEFAULT ''")
        self.db.execute('CREATE TABLE IF NOT EXISTS evidence(trade_id INTEGER REFERENCES trades(id),import_id INTEGER REFERENCES imports(id),UNIQUE(trade_id,import_id))')
        self.db.execute('INSERT OR IGNORE INTO evidence SELECT id,import_id FROM trades')
        account_columns={r[1] for r in self.db.execute('PRAGMA table_info(accounts)')}
        if 'owner_name' not in account_columns:self.db.execute("ALTER TABLE accounts ADD COLUMN owner_name TEXT NOT NULL DEFAULT ''")
        self.db.commit()
        legacy=not self.db.execute("SELECT 1 FROM accounts WHERE id='E'").fetchone()
        if legacy:self._migrate_family_accounts()
        with self.db:
            for account,label in FAMILY_ACCOUNTS.items():
                self.db.execute('INSERT OR IGNORE INTO accounts(id,label) VALUES (?,?)',(account,label))

    def _migrate_family_accounts(self):
        """Move only the known Shota sample evidence from old A to new B."""
        # Keep only the one-way fingerprints here.  The user's original sample
        # files are intentionally not included in distributable ZIPs.
        digests={
            'b5f5d8a5cc76f5e5241e2b036e3b32f54f059b050311f907e949d0abf35c5cc0',
            'b91d146d63c9b339b875caba56c20614281f095fc488f230ac6723569c24678d',
        }
        selected=list(self.db.execute('SELECT id,digest,kind FROM imports WHERE account=?',('A',)))
        move_ids=[r['id'] for r in selected if r['digest'] in digests]
        if move_ids:
            if self.db.execute("SELECT 1 FROM imports WHERE account='B'").fetchone() or self.db.execute("SELECT 1 FROM trades WHERE account='B'").fetchone():
                raise LedgerError('旧版の口座Bに記録があり、将太の記録を安全に移せません。古い data フォルダはそのまま保管し、まず新しい版を別フォルダで起動してください。')
            placeholders=','.join('?' for _ in move_ids)
            trade_ids=[r[0] for r in self.db.execute(f'SELECT id FROM trades WHERE import_id IN ({placeholders})',move_ids)]
            if trade_ids:
                trade_marks=','.join('?' for _ in trade_ids)
                outsider=self.db.execute(f'SELECT 1 FROM evidence WHERE trade_id IN ({trade_marks}) AND import_id NOT IN ({placeholders}) LIMIT 1',trade_ids+move_ids).fetchone()
                if outsider:raise LedgerError('将太の取引に別資料が紐付いています。自動移行を止めました。古い data フォルダを保管してください。')
            self.backup()
            for r in selected:
                if r['id'] in move_ids:
                    source=self.directory/'originals'/'A'/f"{r['digest']}.{r['kind']}"
                    target=self.directory/'originals'/'B'/source.name
                    if source.exists():
                        target.parent.mkdir(parents=True,exist_ok=True)
                        if not target.exists():shutil.copy2(source,target)
            with self.db:
                self.db.execute(f"UPDATE imports SET account='B' WHERE id IN ({placeholders})",move_ids)
                if trade_ids:self.db.execute(f"UPDATE trades SET account='B' WHERE id IN ({trade_marks})",trade_ids)
        with self.db:
            for account,label in FAMILY_ACCOUNTS.items():
                self.db.execute('INSERT OR IGNORE INTO accounts(id,label) VALUES (?,?)',(account,label))
                self.db.execute('UPDATE accounts SET label=? WHERE id=?',(label,account))

    def accounts(self): return [dict(r) for r in self.db.execute('SELECT * FROM accounts ORDER BY id')]

    def rename(self,account,label):
        if not label.strip() or len(label)>50: raise LedgerError('口座名は1〜50文字で入力してください。')
        with self.db:
            cur=self.db.execute('UPDATE accounts SET label=? WHERE id=?',(label.strip(),account))
            if cur.rowcount!=1: raise LedgerError('口座IDが不正です。')

    def set_owner(self,account,name):
        if not name.strip() or len(name)>50:raise LedgerError('PDFに記載された氏名を1〜50文字で入力してください。')
        with self.db:
            cur=self.db.execute('UPDATE accounts SET owner_name=? WHERE id=?',(name.strip(),account))
            if cur.rowcount!=1:raise LedgerError('口座IDが不正です。')

    def backup(self):
        target=self.directory/'backups'/f'ledger_{datetime.now():%Y%m%d_%H%M%S_%f}.sqlite3'
        target.parent.mkdir(exist_ok=True)
        dest=sqlite3.connect(target)
        try:self.db.backup(dest)
        finally:dest.close()
        return target

    def preview(self,account,snapshot):
        if not self.db.execute('SELECT 1 FROM accounts WHERE id=?',(account,)).fetchone(): raise LedgerError('取り込み先の口座を選んでください。')
        duplicate=self.db.execute('SELECT 1 FROM imports WHERE account=? AND digest=?',(account,snapshot.digest)).fetchone()
        if snapshot.kind=='pdf':
            profile=self.db.execute('SELECT label,owner_name FROM accounts WHERE id=?',(account,)).fetchone()
            label=profile['owner_name'] or profile['label']
            if label in ('母','父'):
                raise LedgerError('母・父の取引報告書は、口座名だけでは名義を区別できません。「PDF名義を設定」で報告書に記載された氏名を登録してください。')
            plain=lambda s:re.sub(r'\s+','',unicodedata.normalize('NFKC',s))
            if plain(label) not in plain(snapshot.owner):raise LedgerError(f'PDF名義「{snapshot.owner}」と口座名「{label}」が一致しません。名義を含む口座名を設定してください。')
            duplicate=duplicate or self.db.execute('SELECT 1 FROM imports WHERE account=? AND document_key=?',(account,snapshot.document_key)).fetchone()
        if duplicate:return {'new':0,'existing':len(snapshot.trades),'already':True,'conflicts':[]}
        old_rows=list(self.db.execute('SELECT * FROM trades WHERE account=? AND trade_date BETWEEN ? AND ?',(account,snapshot.start,snapshot.end)))
        old=Counter(trade_key(json.loads(r['payload'])) for r in old_rows)
        incoming=Counter(trade_key(t) for t in snapshot.trades)
        if snapshot.kind=='pdf':
            for key,count in incoming.items():
                matches=[r for r in old_rows if trade_key(json.loads(r['payload']))==key]
                if len(matches)>count:raise LedgerError('同じ条件の取引数が登録済みデータと一致しません。重複登録を防ぐため停止しました。')
                for matched in matches:
                    csv_evidence=self.db.execute("SELECT 1 FROM evidence e JOIN imports i ON i.id=e.import_id WHERE e.trade_id=? AND i.kind='csv'",(matched['id'],)).fetchone()
                    pdf_evidence=self.db.execute("SELECT 1 FROM evidence e JOIN imports i ON i.id=e.import_id WHERE e.trade_id=? AND i.kind='pdf'",(matched['id'],)).fetchone()
                    if not csv_evidence or pdf_evidence:
                        raise LedgerError('同じ条件の取引が存在しますが、同一約定か別約定か確定できません。二重登録を避けるため停止しました。')
            covered=self.db.execute("SELECT 1 FROM imports WHERE account=? AND kind='csv' AND start<=? AND end>=?",(account,snapshot.start,snapshot.end)).fetchone()
            if covered and any(incoming[k]>old[k] for k in incoming):
                raise LedgerError('PDFの取引が、同期間に取り込んだCSVに見つかりません。口座・取引条件の確認が必要です。')
            existing=sum(old[k] for k in incoming)
            return {'new':len(snapshot.trades)-existing,'existing':existing,'already':False,'conflicts':[]}
        conflicts=[{'signature':k,'stored':v,'incoming':incoming[k]} for k,v in old.items() if incoming[k]<v]
        new=sum(max(v-old[k],0) for k,v in incoming.items())
        return {'new':new,'existing':len(snapshot.trades)-new,'already':False,'conflicts':conflicts}

    def import_snapshot(self,account,snapshot):
        preview=self.preview(account,snapshot)
        if preview['already']: return preview
        if preview['conflicts']: raise LedgerError('保存済みの同期間の取引と一致しません。訂正・部分出力・別口座の可能性があります。何も変更していません。原本と口座を確認してください。')
        self.backup()
        archive=self.directory/'originals'/account/f'{snapshot.digest}.{snapshot.kind}'
        archive.parent.mkdir(parents=True,exist_ok=True)
        if not archive.exists():archive.write_bytes(snapshot.raw)
        with self.db:
            ident=self.db.execute('INSERT INTO imports(account,digest,filename,start,end,imported_at,kind,document_key) VALUES (?,?,?,?,?,?,?,?)',
                (account,snapshot.digest,snapshot.filename,snapshot.start,snapshot.end,datetime.now().isoformat(),snapshot.kind,snapshot.document_key)).lastrowid
            old=defaultdict(list)
            for r in self.db.execute('SELECT * FROM trades WHERE account=? ORDER BY id',(account,)):
                old[trade_key(json.loads(r['payload']))].append(r['id'])
            occurrences=Counter()
            for t in snapshot.trades:
                key=trade_key(t);occurrences[key]+=1;ordinal=occurrences[key]
                if ordinal<=len(old[key]):tradeid=old[key][ordinal-1]
                else:
                    tradeid=self.db.execute('INSERT INTO trades(account,signature,occurrence,payload,trade_date,import_id) VALUES (?,?,?,?,?,?)',
                        (account,key,ordinal,json.dumps(t,ensure_ascii=False),t['trade_date'],ident)).lastrowid
                self.db.execute('INSERT OR IGNORE INTO evidence VALUES (?,?)',(tradeid,ident))
        return preview

    def records(self,month=''):
        cutoff=None
        if month:
            if not re.fullmatch(r'\d{4}-\d{2}',month):raise LedgerError('月は2026-05の形式で入力してください。空欄は全期間です。')
            try:
                y,m=map(int,month.split('-'));cutoff=date(y,m,calendar.monthrange(y,m)[1]).isoformat()
            except ValueError as e:raise LedgerError('存在しない年月です。') from e
        rows=self.db.execute('''SELECT t.*,a.label,i.filename FROM trades t JOIN accounts a ON a.id=t.account
          JOIN imports i ON i.id=t.import_id ORDER BY t.trade_date,t.account,t.id''')
        result=[]
        for r in rows:
            if cutoff and r['trade_date']>cutoff:continue
            sources=[v[0] for v in self.db.execute('SELECT i.filename FROM evidence e JOIN imports i ON i.id=e.import_id WHERE e.trade_id=? ORDER BY i.id',(r['id'],))]
            d=json.loads(r['payload']);d.update(record_id=f"{r['account']}-{r['signature'][:20]}-{r['occurrence']}",
                account=r['account'],account_label=r['label'],source=' / '.join(sources));result.append(d)
        return result

    def close(self):self.db.close()
