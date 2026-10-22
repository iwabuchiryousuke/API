"""Stage several same-account PDFs on a temporary ledger before confirmation."""
import hashlib
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from ledger import LedgerError,Store
from activity import Activities
from corporate_actions import parse_notice,parse_balance
from pdf_import import parse_report
from pdf_common import load_pdf


@dataclass(frozen=True)
class DocumentExclusionRule:
    code:str
    title:str
    required_title_parts:tuple[str,...]
    reason:str
    needs_review:bool=True
    identifiers:tuple[str,...]=()
    display_title:str=''


# This finite list describes identified document types, not an instruction to
# ignore arbitrary notices. New financial event parsers require verified PDFs.
# More specific titles precede generic annual-report references in disclosures.
KNOWN_DOCUMENT_EXCLUSIONS=(
    DocumentExclusionRule(
        'FOREIGN_STOCK_CORPORATE_ACTION',
        '外国株式等株式分割・権利売却等のご案内',
        ('外国株式等株式分割・権利売却等のご案内',),
        '国内株式の株数・売却代金には反映しません。PDF原本を保持し、内容・通貨を確認してください。'),
    DocumentExclusionRule(
        'NISA_TRANSFER_OUT',
        '非課税口座内上場株式等払出通知書',
        ('非課税口座内上場株式等払出通知書',),
        '売買約定の明細ではありません。NISAからの払出し・口座区分の移動は未反映です。'
        '売却取引・税額・取得単価を推定せず、PDF原本を保持します。原本と既存の預り区分・取得価額を確認してください。'),
    DocumentExclusionRule(
        'FOREIGN_BOND_INCOME',
        '外国債券利金・償還金お支払のご案内',
        ('外国債券利金','償還金お支払のご案内'),
        '円額と利金部分を原本で確認し、利息は手動登録してください。PDF原本を保持します。',
        display_title='外国債券の利金・償還金'),
    DocumentExclusionRule(
        'ANNUAL_TAX_REPORT',
        '特定口座年間取引報告書',
        ('特定口座年間取引報告書',),
        '売買明細ではないため、取引として重ねて登録しません。PDF原本を保持します。',
        needs_review=False,identifiers=('STWHGB',)),
    DocumentExclusionRule(
        'FOREIGN_STOCK_TRADE',
        '外国株式等取引報告書',
        ('外国株式等取引報告書',),
        '外国株式の売買・通貨別金額は取引台帳へ未反映です。推定で円建て取引を作らず、PDF原本を保持します。'),
    DocumentExclusionRule(
        'FOREIGN_BOND_TRADE',
        '外国債券取引報告書',
        ('外国債券取引報告書',),
        '外国債券の売買・通貨別金額は取引台帳へ未反映です。推定で円建て取引を作らず、PDF原本を保持します。'),
)


class KnownDocumentExclusion(LedgerError):
    """An identified but unimported document, with a reviewable reason."""
    def __init__(self,rule):
        self.code=rule.code
        self.document_title=rule.title
        self.reason=rule.reason
        self.needs_review=rule.needs_review
        title=rule.display_title or rule.title
        status='（未解析）' if rule.needs_review else '（売買明細ではありません）'
        super().__init__('対象外：'+title+status+'。'+rule.reason)


def _known_exclusion(first):
    for rule in KNOWN_DOCUMENT_EXCLUSIONS:
        if all(part in first for part in rule.required_title_parts) or any(marker in first for marker in rule.identifiers):
            return KnownDocumentExclusion(rule)
    return None


def classify(path):
    data=load_pdf(path,max_pages=50)
    text=data.best_compact+data.compact
    first=data.pages[0].best_compact+data.pages[0].compact
    if 'A501' in text and '取引報告書' in text:
        from smbc_import import parse_smbc
        return 'trade',parse_smbc(path)
    # A balance report can mention bond interest, redemption and annual reports
    # in its disclosures. Its own first-page report identifier takes priority
    # over those incidental words; none of them changes the document's type.
    if any(marker in first for marker in ('STWHZB','FR00011M01')):
        return 'balance',parse_balance(path)
    # Recognize first-page content/identifiers, never downloaded file names.
    # Incidental references on later pages cannot exclude a supported report.
    exclusion=_known_exclusion(first)
    if exclusion is not None:raise exclusion
    if ('株式等配当金のお知らせ' in text or
            '株式等利益剰余金配当金のお知らせ' in text or
            '割当株式等のお知らせ' in text or
            '割当株券のお知らせ' in text or
            '譲渡益税徴収・還付のお知らせ' in text):
        return 'notice',parse_notice(path)
    if any(marker in text for marker in
           ('取引残高報告書','お預り残高等の明細','STWHZB','FR00011M01')):
        return 'balance',parse_balance(path)
    if '株式取引報告書' in text or '投資信託取引報告書' in text:
        return 'trade',parse_report(path)
    raise LedgerError('対応する国内株式・投資信託の取引報告書、配当・分割・税還付通知、取引残高報告書ではありません。')

def _apply(store,actions,account,kind,doc):
    if kind=='trade':
        preview=store.preview(account,doc)
        if preview['conflicts']:raise LedgerError('登録済み取引と一致しません。')
        store.import_snapshot(account,doc)
        return preview['new'],preview['existing'],preview['already']
    if kind=='notice':
        preview=actions.preview_notice(account,doc)
        actions.import_notice(account,doc)
        return (0 if preview['already'] or preview['same_event'] else 1),int(preview['same_event']),preview['already']
    exists=actions.db.execute('SELECT 1 FROM balance_reports WHERE account=? AND digest=?',(account,doc.digest)).fetchone()
    actions.import_balance(account,doc)
    return int(not exists),0,bool(exists)

def preflight(store,account,paths,skip_unreadable=False):
    if not paths:raise LedgerError('PDFが選ばれていません。')
    # A copied DB lets later PDFs see earlier ones, while the real DB stays untouched.
    with tempfile.TemporaryDirectory() as temp:
        data=Path(temp)/'data';data.mkdir()
        copied=sqlite3.connect(data/'ledger.sqlite3')
        try:store.db.backup(copied)
        finally:copied.close()
        staged=Store(data);activities=Activities(staged)
        # This database is discarded after the preview; backups here only leave
        # open SQLite files that Windows cannot remove with the temp directory.
        staged.backup=lambda:None
        result=[];skipped=[]
        try:
            for path in paths:
                path=Path(path)
                try:
                    try:kind,doc=classify(path)
                    except (LedgerError,OSError,ValueError) as e:
                        if not skip_unreadable:raise
                        skipped.append((path,str(e)))
                        continue
                    added,existing,already=_apply(staged,activities,account,kind,doc)
                except (LedgerError,OSError,ValueError) as e:
                    raise LedgerError(f'{path.name}：{e}。実データは変更していません。') from e
                result.append((path,kind,doc,added,existing,already))
        finally:staged.close()
    return (result,skipped) if skip_unreadable else result

def import_all(store,account,pending):
    actions=Activities(store)
    for path,kind,doc,*_ in pending:
        if hashlib.sha256(path.read_bytes()).hexdigest()!=doc.digest:
            raise LedgerError(f'{path.name} が確認後に変更されました。最初からやり直してください。')
    results=[]
    # One recovery image covers the entire confirmed batch. Original evidence
    # may remain as an unreferenced file after rollback, but no ledger row does.
    with tempfile.TemporaryDirectory() as temp:
        before=sqlite3.connect(str(Path(temp)/'before.sqlite3'))
        store.db.backup(before)
        try:
            for path,kind,doc,*_ in pending:results.append(_apply(store,actions,account,kind,doc))
        except Exception:
            store.db.rollback();before.backup(store.db)
            raise LedgerError('一括登録の途中で問題が起きたため、台帳全体を登録前に戻しました。原本・空き容量を確認してください。') from None
        finally:before.close()
    return results
