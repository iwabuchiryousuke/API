"""One coherent update: collect -> stage all accounts -> review -> commit -> export.
No live ledger writes until every source has passed staging and review.
Reports are built before commit. The job records the exact completion phase.
"""
from contextlib import contextmanager,ExitStack
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass,asdict
import hashlib,json,os,re,shutil,sqlite3,stat,tempfile,uuid
from ledger import Store,LedgerError
from login_credentials import CredentialError
from activity import Activities
from asset_snapshots import init as init_assets
from pdf_batch import classify,_apply,ArchivedUnparsedDocument
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

_RESEARCH_FILE=re.compile(r'research_([0-9]{4})\.json\Z')
_RESEARCH_LIMIT=4*1024*1024

def _ordinary_reference(path,directory=False):
    if path.exists() or path.is_symlink():
        info=path.lstat()
        if (path.is_symlink() or getattr(info,'st_file_attributes',0)&getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0)
                or (not path.is_dir() if directory else not stat.S_ISREG(info.st_mode) or info.st_nlink!=1)):
            raise OSError('通常の公開株価キャッシュを使用してください。')

def _research_contents(directory):
    market=Path(directory)/'market';_ordinary_reference(market,True)
    if not market.exists():return {}
    contents={}
    for path in market.iterdir():
        if not _RESEARCH_FILE.fullmatch(path.name):continue
        _ordinary_reference(path)
        if path.stat().st_size>_RESEARCH_LIMIT:raise OSError('公開株価キャッシュのサイズを確認できません。')
        contents[path.name]=path.read_bytes()
    return contents

def _reference_hashes(contents):return {name:hashlib.sha256(raw).hexdigest() for name,raw in contents.items()}

def _restore_research(directory,contents):
    market=Path(directory)/'market';_ordinary_reference(market,True)
    market.mkdir(exist_ok=True)
    for path in market.iterdir():
        if _RESEARCH_FILE.fullmatch(path.name) and path.name not in contents:
            _ordinary_reference(path);path.unlink()
    for name,raw in contents.items():
        path=market/name;_ordinary_reference(path);path.write_bytes(raw)

def _publish_reference_cache(stage_directory,live_directory,initial_hashes):
    """Best-effort public cache publication after the financial commit.

    Only research_YYYY.json changes are copied. Concurrently edited public
    facts are kept; opaque settings and the ledger are never accessed here.
    """
    results=dict(written=[],conflicts=[],failed=[])
    try:contents=_research_contents(stage_directory)
    except (OSError,ValueError):results['failed'].append('reference-cache');return results
    live_market=Path(live_directory)/'market'
    for name,raw in contents.items():
        digest=hashlib.sha256(raw).hexdigest()
        if digest==initial_hashes.get(name):continue
        temporary=None
        try:
            value=json.loads(raw.decode('utf-8'))
            year=int(_RESEARCH_FILE.fullmatch(name)[1])
            if (not isinstance(value,dict) or type(value.get('year')) is not int
                    or value['year']!=year or not isinstance(value.get('securities'),dict)):
                raise ValueError('公開株価の年・銘柄形式を確認できません。')
            _ordinary_reference(live_market,True);live_market.mkdir(exist_ok=True)
            target=live_market/name
            def unchanged():
                _ordinary_reference(target)
                actual=source_digest(target) if target.exists() else None
                return actual==initial_hashes.get(name)
            if not unchanged():results['conflicts'].append(name);continue
            with tempfile.NamedTemporaryFile(prefix='.reference_',suffix='.tmp',dir=live_market,delete=False) as handle:
                temporary=Path(handle.name);handle.write(raw);handle.flush();os.fsync(handle.fileno())
            if not unchanged():results['conflicts'].append(name);continue
            os.replace(temporary,target);results['written'].append(name)
        except (OSError,ValueError,TypeError,UnicodeError):results['failed'].append(name)
        finally:
            if temporary is not None:
                try:temporary.unlink(missing_ok=True)
                except OSError:pass
    return results

@dataclass
class Summary:
    new_pdf_files:int=0
    known_pdf_files:int=0
    excluded_pdf_files:int=0
    new_trades:int=0
    new_cash_or_splits:int=0
    balance_reports:int=0
    assets_observed:int=0
    recovered_asset_accounts:int=0
    daily_asset_records:int=0

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
    def run(self,month='',collect=None,review=None,include_father=True,accounts=None,include_saved_pdfs=True,current_only=False,reference_refresh=None):
        """collect(account, stage_store) returns structured outcome; review receives
        the full proposal and must return True. Offline reuse never pretends a
        live collection was performed. Father is never logged in automatically.
        """
        try:
            with exclusive(self.base/'data'/'update.lock'):
                return self._run(month,collect,review,include_father,accounts,include_saved_pdfs,current_only,reference_refresh)
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
    def _run(self,month,collect,review,include_father,accounts=None,include_saved_pdfs=True,current_only=False,reference_refresh=None):
        from acquisition_targets import target_dates
        self.target_dates=() if current_only else target_dates(month)
        selected=tuple(accounts) if accounts is not None else tuple("ABCDE" if include_father else "ABCD")
        if not selected or len(set(selected))!=len(selected) or any(a not in "ABCDE" for a in selected):raise LedgerError("対象口座が不正です。")
        if current_only and collect and not set('ABCD')<=set(selected):raise LedgerError('現在の現金・NISA取得はA～Dの4口座をまとめて実行してください。')
        self.state['mode']='current' if current_only else 'legacy'
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
                initial_research={};reference_ready=True;reference_succeeded=False
                if current_only:
                    try:
                        public_research=_research_contents(live.directory)
                        initial_research=_reference_hashes(public_research)
                    except (OSError,ValueError):
                        reference_ready=False;self.state['warnings'].append('REFERENCE_PRICES_UNAVAILABLE')
                    if reference_ready and public_research:
                        staged_market=stage.directory/'market';staged_market.mkdir()
                        for name,raw in public_research.items():(staged_market/name).write_bytes(raw)
                elif (live.directory/'market').is_dir():shutil.copytree(live.directory/'market',stage.directory/'market')
                for filename in ('security_names.json',):
                    if (live.directory/filename).exists():shutil.copy2(live.directory/filename,stage.directory/filename)
                summary=Summary();proposal=[];sources=[]
                from acquisition_journal import replay_captures
                # Current acquisition does not replay old historical snapshots
                # or treat an earlier observation as a fresh current value.
                replayed=({'accounts':[],'windows':{},'warnings':[],'proposal':[]} if current_only else
                          replay_captures(self.base,stage,accounts=selected))
                summary.recovered_asset_accounts=len(replayed['accounts'])
                self.state['warnings'].extend(replayed['warnings'])
                proposal.extend(replayed['proposal'])
                if collect:
                    collection_errors=[]
                    current_accounts={}
                    from sbi_views import _jst_now
                    attempted_at=_jst_now()
                    owners={a['id']:a.get('owner_name','') for a in stage.accounts()}
                    for account in (a for a in selected if a!='E'):
                        self.check_cancel();self.emit('COLLECTING',account)
                        if current_only:
                            from current_capture import observation_identities,account_capture
                            identities=observation_identities(stage,account);since=_jst_now()
                        try:result=collect(account,stage)
                        except CredentialError as e:
                            self.check_cancel()
                            collection_errors.append(str(e));self.progress(phase='ACCOUNT_FAILED',account=account);continue
                        self.check_cancel()
                        if not isinstance(result,dict) or not result.get('complete'):
                            raise UpdateError('COLLECTION_INCOMPLETE',f'口座{account}の収集が完了していないため、登録を止めました。')
                        if current_only:
                            captured=account_capture(stage,account,owners[account],identities,since,_jst_now())
                            current_accounts[account]=captured
                            missing=captured['missing']
                            if not missing:summary.assets_observed+=1
                            else:self.state['warnings'].append(f'ASSETS_NOT_OBSERVED_{account}')
                        else:
                            if result.get('assets_observed'):summary.assets_observed+=1
                            else:self.state['warnings'].append(f'ASSETS_NOT_OBSERVED_{account}')
                            missing=result.get('assets_missing',[])
                        if missing:self.state['warnings'].append(f'ASSET_FIELDS_MISSING:{account}:'+','.join(missing))
                    if collection_errors:raise UpdateError('COLLECTION_INCOMPLETE','収集を完了できなかった口座があります。保存済みPDFは保持しています。\n'+'\n'.join(collection_errors))
                    if current_only:
                        from current_capture import save_current_capture,FIELD_LABELS
                        current_batch=save_current_capture(stage,self.job_id,current_accounts,attempted_at)
                        self.state['current_capture']=dict(batch_id=current_batch['batch_id'],captured_at=current_batch['captured_at'])
                        for account,captured in current_accounts.items():
                            proposal.append(dict(account=account,name='取得時の円・米ドル現金とNISA枠',kind='現在値',new=0,existing=0,
                                state='取得済み' if not captured['missing'] else '未取得項目：'+'・'.join(FIELD_LABELS[key] for key in captured['missing'])))
                else:self.state['warnings'].append('SAVED_FILES_ONLY')
                if collect and not current_only:
                    # Include freshly persisted captures in the same validated
                    # grouping used by an offline retry; replay is idempotent.
                    fresh=replay_captures(self.base,stage,accounts=selected)
                    replayed['windows']=fresh['windows']
                    self.state['warnings'].extend(w for w in fresh['warnings'] if w not in self.state['warnings'])
                from asset_history import stage_boundary_observations
                for account in (() if current_only else (a for a in selected if a!='E')):
                    daily=stage_boundary_observations(stage,account,self.target_dates,acquisition_windows=replayed['windows'])
                    summary.daily_asset_records+=daily['count']
                    proposal.extend(daily['proposal'])
                    self.state['warnings'].extend(daily['warnings'])
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
                            if isinstance(e,ArchivedUnparsedDocument) or message.startswith(('対象外：','解析済み：')):
                                owner=getattr(e,'owner',None)
                                if owner:activities._owner(account,owner)
                                summary.excluded_pdf_files+=1
                                proposal.append({'account':account,'name':path.name,'kind':'保管のみ' if isinstance(e,ArchivedUnparsedDocument) else '対象外','new':0,'existing':0,'state':message})
                                self.state.setdefault('retained_pdfs',[]).append(dict(account=account,name=path.name,
                                    sha256=digest,code=getattr(e,'code','NON_TRANSACTION'),reason=message))
                                if getattr(e,'needs_review',False) or '未解析' in message or any(word in message for word in ('外国株式','外国債券')):
                                    warning=f'PDF_NOT_PARSED:{account}:{path.name}'
                                    if warning not in self.state['warnings']:self.state['warnings'].append(warning)
                                if getattr(e,'code',None)=='NISA_TRANSFER_OUT':
                                    self.state['warnings'].append(f'NISA_TRANSFER_UNAPPLIED:{account}:{path.name}')
                                continue
                            raise UpdateError('UNSUPPORTED_PDF',f'口座{account}：{path.name}\n{message}\nPDFの破損・読取環境・整合性を確認できないため、一括登録を停止しました。原本は保持しています。') from None
                        if doc.digest!=digest:raise UpdateError('SOURCE_CHANGED','検査中にPDFが変更されました。もう一度実行してください。')
                        added,existing,already=_apply(stage,activities,account,kind,doc);known.add(digest)
                        summary.new_pdf_files+=not already
                        proposal.append({'account':account,'name':path.name,'kind':kind,'new':added,'existing':existing,'state':'名義・整合性確認済み'})
                after=(len(stage.records()),len(activities.events()),len(activities.balances()))
                summary.new_trades=after[0]-before[0];summary.new_cash_or_splits=after[1]-before[1];summary.balance_reports=after[2]-before[2]
                if current_only and collect and reference_refresh is not None and reference_ready:
                    self.check_cancel();self.emit('REFERENCE_PRICES')
                    original_research=None
                    try:
                        original_research=_research_contents(stage.directory)
                        reference_result=reference_refresh(stage,month)
                        reference_succeeded=True
                        if isinstance(reference_result,dict) and reference_result.get('failed'):
                            self.state['warnings'].append('REFERENCE_PRICES_UNAVAILABLE')
                    except Cancelled:raise
                    except Exception:
                        if original_research is not None:
                            try:_restore_research(stage.directory,original_research)
                            except (OSError,ValueError):pass
                        self.state['warnings'].append('REFERENCE_PRICES_UNAVAILABLE')
                    self.check_cancel()
                from asset_snapshots import historical_status,snapshots
                dated={} if current_only else snapshots(stage)
                for account in (() if current_only else (a for a in selected if a!='E')):
                    history=historical_status(stage,account,self.target_dates)
                    for target,status in history.items():
                        row=dated.get((account,target),{})
                        if row.get('source')=='SBI My資産の前月比':
                            self.state['warnings'].append(f'ASSET_DATE_LEGACY_ESTIMATE:{account}:{target}')
                        if row.get('evidence',{}).get('method')=='observed-at':
                            self.state['warnings'].append(f'ASSET_DATE_OBSERVED:{account}:{target}')
                        if not status['complete']:
                            self.state['warnings'].append(f'ASSET_DATE_INCOMPLETE:{account}:{target}')
                self.emit('PREPARING_EXCEL',summary=asdict(summary));self.check_cancel()
                summary_data=asdict(summary)
                summary_data['warnings']=list(self.state['warnings'])
                # Build with production exporter before committing anything.
                prepared=export_trades(stage,self.job/'prepared',month,activities)
                # Reference research is public, and must be the user's current copy.
                # Staging copies this in run preparation below, never private files.
                if review is None or not review(summary_data,proposal,prepared):raise Cancelled()
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
                if reference_succeeded:
                    # Price-cache failures cannot undo or falsely fail an
                    # already committed financial update.
                    try:published=_publish_reference_cache(stage.directory,live.directory,initial_research)
                    except Exception:published=dict(written=[],conflicts=[],failed=['reference-cache'])
                    self.state['reference_cache']=published
                    if published['conflicts']:self.state['warnings'].append('REFERENCE_CACHE_CONFLICT')
                    if published['failed']:self.state['warnings'].append('REFERENCE_CACHE_UNSAVED')
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
            tables=TABLES
            if stage.db.execute("SELECT 1 FROM sqlite_master WHERE name='current_acquisitions'").fetchone():
                from current_capture import init_current_capture
                init_current_capture(live,commit=False)
                tables=tables+('current_acquisitions',)
            for table in tables:
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
                        if table=='current_acquisitions':raise UpdateError('STAGING_CHANGED_CAPTURE','既存の取得時点の記録を書き換える変更を検出したため停止しました。')
                        if table=='account_observations' and self.state.get('mode')=='current':raise UpdateError('STAGING_CHANGED_OBSERVATION','登録済みの取得観測値を書き換える変更を検出したため停止しました。')
                        nonkeys=[c for c in columns if c not in keys]
                        live.db.execute(f'UPDATE {table} SET '+','.join(c+'=?' for c in nonkeys)+' WHERE '+' AND '.join(c+'=?' for c in keys),[row[c] for c in nonkeys]+list(key))
            if live.db.execute('PRAGMA foreign_key_check').fetchone():raise UpdateError('RELATIONSHIP_ERROR','関連データの整合性を確認できませんでした。')
            live.db.commit()
        except BaseException:live.db.rollback();raise
