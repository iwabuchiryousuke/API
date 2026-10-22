"""One coherent update: collect -> stage all accounts -> review -> commit -> export.
No live ledger writes until every source has passed staging and review.
Reports are built before commit. The job records the exact completion phase.
"""
from contextlib import contextmanager,ExitStack
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass,asdict
import hashlib,json,os,shutil,sqlite3,tempfile,uuid
from ledger import Store,LedgerError
from login_credentials import CredentialError
from activity import Activities
from asset_snapshots import init as init_assets
from pdf_batch import classify,_apply
from standalone_excel import export_trades
from version import APP_VERSION

TABLES=('accounts','imports','trades','evidence','activities','activity_files','balance_reports','asset_snapshots','account_observations')
class UpdateError(LedgerError):
    def __init__(self,code,message):super().__init__(message);self.code=code
class Cancelled(UpdateError):
    def __init__(self):super().__init__('CANCELLED','更新を中止しました。取引台帳は変更していません。')

@contextmanager
def exclusive(path):
    """OS releases this lock even after process termination; no stale PID file."""
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    handle=path.open('a+b');handle.seek(0);handle.write(b'0');handle.flush();handle.seek(0)
    try:
        try:
            if os.name=='nt':
                import msvcrt;msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl;fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except (OSError,BlockingIOError):raise UpdateError('BUSY','別の更新処理が実行中です。終了してから実行してください。') from None
        yield
    finally:handle.close()

def fingerprint(db):
    return hashlib.sha256('\n'.join(db.iterdump()).encode()).hexdigest()

def source_digest(path):
    with Path(path).open('rb') as f:
        h=hashlib.sha256()
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
        return h.hexdigest()

@dataclass
class Summary:
    new_pdf_files:int=0
    known_pdf_files:int=0
    excluded_pdf_files:int=0
    new_trades:int=0
    new_cash_or_splits:int=0
    balance_reports:int=0
    assets_observed:int=0

class Workflow:
    def __init__(self,base,progress=None,cancelled=None):
        self.base=Path(base);self.progress=progress or (lambda **kw:None);self.cancelled=cancelled or (lambda:False)
        self.job_id=datetime.now().strftime('%Y%m%d_%H%M%S_')+uuid.uuid4().hex[:8]
        self.job=self.base/'data'/'jobs'/self.job_id;self.job.mkdir(parents=True)
        self.committed=False
        self.state={'id':self.job_id,'version':APP_VERSION,'phase':'CREATED','account':'','warnings':[]}
    def emit(self,phase,account='',**counts):
        self.state.update(phase=phase,account=account,**counts)
        temp=self.job/'state.tmp';temp.write_text(json.dumps(self.state,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(temp,self.job/'state.json')
        self.progress(phase=phase,account=account,**counts)
    def check_cancel(self):
        if self.cancelled():raise Cancelled()
    def run(self,month='',collect=None,review=None,include_father=True,accounts=None,include_saved_pdfs=True):
        """collect(account, stage_store) returns structured outcome; review receives
        the full proposal and must return True. Offline reuse never pretends a
        live collection was performed. Father is never logged in automatically.
        """
        try:
            with exclusive(self.base/'data'/'update.lock'):
                return self._run(month,collect,review,include_father,accounts,include_saved_pdfs)
        except UpdateError as e:
            self.emit('CANCELLED' if isinstance(e,Cancelled) else 'FAILED',error_code=e.code)
            raise
        except CredentialError:
            self.emit('FAILED',error_code='AUTHENTICATION');raise
        except LedgerError:
            self.emit('FAILED',error_code='INPUT_VALIDATION');raise
        except Exception:
            if self.committed:
                self.emit('OUTPUT_PENDING',error_code='EXPORT_MOVE')
                raise UpdateError('OUTPUT_PENDING','台帳登録は完了しましたが、出力先へExcelを置けませんでした。「保存分をまとめて更新」で再出力できます。') from None
            self.emit('FAILED',error_code='UNEXPECTED')
            raise UpdateError('UNEXPECTED','更新処理を停止しました。台帳は更新前の状態です。空き容量・入力ファイル・設定を確認してください。') from None
    def _run(self,month,collect,review,include_father,accounts=None,include_saved_pdfs=True):
        selected=tuple(accounts) if accounts is not None else tuple("ABCDE" if include_father else "ABCD")
        if not selected or len(set(selected))!=len(selected) or any(a not in "ABCDE" for a in selected):raise LedgerError("対象口座が不正です。")
        live=Store(self.base/'data');stage=None
        try:
            Activities(live);init_assets(live);live.records(month)
            from sbi_views import init_observations
            init_observations(live)
            original=fingerprint(live.db)
            before=(len(live.records()),len(Activities(live).events()),len(Activities(live).balances()))
            with tempfile.TemporaryDirectory(prefix='staging_',dir=self.job) as scratch, ExitStack() as cleanup:
                scratch=Path(scratch);stage=Store(scratch/'data');cleanup.callback(stage.close);live.db.backup(stage.db)
                activities=Activities(stage);stage.backup=lambda:None
                if (live.directory/'market').is_dir():shutil.copytree(live.directory/'market',stage.directory/'market')
                for filename in ('security_names.json',):
                    if (live.directory/filename).exists():shutil.copy2(live.directory/filename,stage.directory/filename)
                summary=Summary();proposal=[];sources=[]
                if collect:
                    collection_errors=[]
                    for account in (a for a in selected if a!='E'):
                        self.check_cancel();self.emit('COLLECTING',account)
                        try:result=collect(account,stage)
                        except CredentialError as e:
                            self.check_cancel()
                            collection_errors.append(str(e));self.progress(phase='ACCOUNT_FAILED',account=account);continue
                        self.check_cancel()
                        if not isinstance(result,dict) or not result.get('complete'):
                            raise UpdateError('COLLECTION_INCOMPLETE',f'口座{account}の収集が完了していないため、登録を止めました。')
                        if result.get('assets_observed'):summary.assets_observed+=1
                        else:self.state['warnings'].append(f'ASSETS_NOT_OBSERVED_{account}')
                        missing=result.get('assets_missing',[])
                        if missing:self.state['warnings'].append(f'ASSET_FIELDS_MISSING:{account}:'+','.join(missing))
                        from asset_snapshots import snapshots,KEYS
                        from report_model import cutoff
                        from datetime import date,timedelta
                        first=date.fromisoformat(cutoff(month)).replace(day=1)
                        stored=snapshots(stage)
                        for target in ((first-timedelta(days=1)).isoformat(),first.isoformat()):
                            if any(stored.get((account,target),{}).get(k) is None for k in KEYS):
                                self.state['warnings'].append(f'ASSET_DATE_INCOMPLETE:{account}:{target}')
                    if collection_errors:raise UpdateError('COLLECTION_INCOMPLETE','収集を完了できなかった口座があります。保存済みPDFは保持しています。\n'+'\n'.join(collection_errors))
                else:self.state['warnings'].append('SAVED_FILES_ONLY')
                for account in (selected if include_saved_pdfs else ()):
                    self.check_cancel();self.emit('CHECKING_PDF',account)
                    known={r[0] for table in ('imports','activity_files','balance_reports') for r in stage.db.execute(f'SELECT digest FROM {table} WHERE account=?',(account,))}
                    paths=sorted((self.base/'data'/'電子ポストPDF'/account).glob('*.pdf'))
                    for path in paths:
                        self.check_cancel();digest=source_digest(path);sources.append((path,digest))
                        if digest in known:
                            summary.known_pdf_files+=1;continue
                        try:kind,doc=classify(path)
                        except LedgerError as e:
                            message=str(e)
                            if message.startswith(('対象外：','解析済み：徴収・還付額0円')):
                                summary.excluded_pdf_files+=1
                                proposal.append({'account':account,'name':path.name,'kind':'対象外','new':0,'existing':0,'state':message})
                                continue
                            raise UpdateError('UNSUPPORTED_PDF',f'口座{account}：{path.name}\n{message}\n未確認の書類を黙って除外せず、今回の一括登録を停止しました。') from None
                        if doc.digest!=digest:raise UpdateError('SOURCE_CHANGED','検査中にPDFが変更されました。もう一度実行してください。')
                        added,existing,already=_apply(stage,activities,account,kind,doc);known.add(digest)
                        summary.new_pdf_files+=not already
                        proposal.append({'account':account,'name':path.name,'kind':kind,'new':added,'existing':existing,'state':'名義・整合性確認済み'})
                after=(len(stage.records()),len(activities.events()),len(activities.balances()))
                summary.new_trades=after[0]-before[0];summary.new_cash_or_splits=after[1]-before[1];summary.balance_reports=after[2]-before[2]
                self.emit('PREPARING_EXCEL',summary=asdict(summary));self.check_cancel()
                # Build with production exporter before committing anything.
                prepared=export_trades(stage,self.job/'prepared',month,activities)
                # Reference research is public, and must be the user's current copy.
                # Staging copies this in run preparation below, never private files.
                if review is None or not review(asdict(summary),proposal,prepared):raise Cancelled()
                self.check_cancel();self.emit('COMMITTING')
                for path,digest in sources:
                    if not path.is_file() or source_digest(path)!=digest:raise UpdateError('SOURCE_CHANGED','確認後に入力ファイルが変更されました。登録していません。')
                # Archive new evidence first; a failure leaves at most unreferenced
                # originals. Existing evidence is never overwritten.
                for p in (stage.directory/'originals').rglob('*'):
                    if not p.is_file():continue
                    target=live.directory/'originals'/p.relative_to(stage.directory/'originals');target.parent.mkdir(parents=True,exist_ok=True)
                    if target.exists():
                        if source_digest(target)!=source_digest(p):raise UpdateError('ARCHIVE_CONFLICT','保存済み原本の内容が一致しません。上書きせず停止しました。')
                    else:
                        temp=target.with_suffix(target.suffix+'.tmp');shutil.copyfile(p,temp);os.replace(temp,target)
                backup=live.backup()
                self._commit(live,stage,original)
                self.committed=True
                self.emit('DATA_COMMITTED',backup=backup.name,summary=asdict(summary))
                target=self.base/'output'/prepared.name;target.parent.mkdir(parents=True,exist_ok=True)
                if target.exists():target=target.with_name(target.stem+'_'+self.job_id+target.suffix)
                os.replace(prepared,target)
                self.emit('COMPLETED_WITH_WARNINGS' if self.state['warnings'] else 'COMPLETED',output=target.name,summary=asdict(summary))
                return {'output':target,'summary':asdict(summary),'warnings':list(self.state['warnings']),'job':self.job}
        finally:
            if stage:stage.close()
            live.close()
    def _commit(self,live,stage,expected):
        """Append/update staged rows inside ONE SQLite transaction. Never delete
        existing rows, replace the entire database, or lose unknown future tables.
        """
        live.db.execute('BEGIN IMMEDIATE')
        try:
            if fingerprint(live.db)!=expected:raise UpdateError('LEDGER_CHANGED','確認中に台帳が別の操作で更新されました。今回の登録を止めました。再実行してください。')
            for table in TABLES:
                info=list(live.db.execute(f'PRAGMA table_info({table})'))
                columns=[r['name'] for r in info]
                keys=[r['name'] for r in sorted(info,key=lambda r:r['pk']) if r['pk']]
                keys=keys or columns # evidence has a composite UNIQUE constraint
                old={tuple(r[k] for k in keys):dict(r) for r in live.db.execute(f'SELECT * FROM {table}')}
                staged={tuple(r[k] for k in keys):dict(r) for r in stage.db.execute(f'SELECT * FROM {table}')}
                if old.keys()-staged.keys():raise UpdateError('STAGING_DELETED_ROWS','検査用データから既存記録が欠落しているため停止しました。')
                for key,row in staged.items():
                    if key not in old:
                        live.db.execute(f'INSERT INTO {table} ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})',[row[c] for c in columns])
                    elif row!=old[key]:
                        if table=='trades':raise UpdateError('STAGING_CHANGED_TRADE','既存の売買記録を書き換える変更を検出したため停止しました。')
                        nonkeys=[c for c in columns if c not in keys]
                        live.db.execute(f'UPDATE {table} SET '+','.join(c+'=?' for c in nonkeys)+' WHERE '+' AND '.join(c+'=?' for c in keys),[row[c] for c in nonkeys]+list(key))
            if live.db.execute('PRAGMA foreign_key_check').fetchone():raise UpdateError('RELATIONSHIP_ERROR','関連データの整合性を確認できませんでした。')
            live.db.commit()
        except BaseException:live.db.rollback();raise
