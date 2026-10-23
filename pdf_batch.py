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


# Identified non-imported types have specific reasons. Other valid PDFs are
# retained separately too; financial entries still require a verified parser.
# More specific titles precede generic annual-report references in disclosures.
KNOWN_DOCUMENT_EXCLUSIONS=(
    DocumentExclusionRule(
        'CUMULATIVE_NISA_BASIS',
        '特定累積投資勘定基準額等通知書',
        ('特定累積投資勘定基準額等通知書',),
        '勘定の基準額等のお知らせで、売買約定・配当入金の明細ではありません。'
        'PDF原本を保持し、取引集計には重ねて登録しません。',needs_review=False),
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


class ArchivedUnparsedDocument(LedgerError):
    """A validated original that contributes no guessed financial entries."""
    needs_review=True
    def __init__(self,reason,code='UNPARSED_PDF',document_title='未対応のPDF書類'):
        self.code=code
        self.document_title=document_title
        self.reason=reason
        self.owner=None
        super().__init__('保管のみ・未反映：'+reason+' PDF原本を保持し、この書類の金額・取引は集計しません。')


def _with_owner(error,data):
    # A positively readable owner is checked even when the financial layout is
    # unsupported. A missing name never authorizes financial registration.
    try:
        from pdf_import import _owner
        error.owner=_owner(data.pages[0].best_text or data.pages[0].text)
    except (LedgerError,AttributeError,IndexError):error.owner=None
    return error


def _load_archive(path):
    try:return load_pdf(path,max_pages=50)
    except LedgerError as error:
        if str(error).startswith('PDF機能の準備'):raise
        # An image-only or long original can be archived without becoming a
        # financial event. Invalid/truncated/non-PDF content remains fatal.
        try:
            raw=Path(path).read_bytes()
            from epost_collector import validate_pdf
            validate_pdf(raw)
        except (OSError,ValueError):raise error from None
        raise ArchivedUnparsedDocument('本文・様式を自動解析できません。原本の内容を確認してください。',
            code='UNREADABLE_VALID_PDF') from None


def _parse(path,data,kind,parser):
    try:return kind,parser(path)
    except LedgerError as error:
        message=str(error)
        if message.startswith('解析済み：'):raise
        if '名義' in message and any(word in message for word in ('一致しません','一致しない','異な')):raise
        raise _with_owner(ArchivedUnparsedDocument(
            '対応候補の書類ですが、明細の形式・項目を確認できません。理由：'+message,
            code='PARSER_UNCONFIRMED',document_title={'trade':'取引報告書','notice':'配当・分割等通知','balance':'取引残高報告書'}[kind]),data) from None
    except ValueError:
        raise _with_owner(ArchivedUnparsedDocument(
            '対応候補の書類ですが、日付・数値等を確認できません。原本を確認してください。',
            code='PARSER_UNCONFIRMED'),data) from None


def _known_exclusion(first):
    for rule in KNOWN_DOCUMENT_EXCLUSIONS:
        if all(part in first for part in rule.required_title_parts) or any(marker in first for marker in rule.identifiers):
            return KnownDocumentExclusion(rule)
    return None


def classify(path):
    data=_load_archive(path)
    text=data.best_compact+data.compact
    first=data.pages[0].best_compact+data.pages[0].compact
    if 'A501' in text and '取引報告書' in text:
        from smbc_import import parse_smbc
        return _parse(path,data,'trade',parse_smbc)
    # A balance report can mention bond interest, redemption and annual reports
    # in its disclosures. Its own first-page report identifier takes priority
    # over those incidental words; none of them changes the document's type.
    if any(marker in first for marker in ('STWHZB','FR00011M01')):
        return _parse(path,data,'balance',parse_balance)
    # Recognize first-page content/identifiers, never downloaded file names.
    # Incidental references on later pages cannot exclude a supported report.
    exclusion=_known_exclusion(first)
    if exclusion is not None:raise _with_owner(exclusion,data)
    if ('株式等配当金のお知らせ' in text or
            '株式等利益剰余金配当金のお知らせ' in text or
            '割当株式等のお知らせ' in text or
            '割当株券のお知らせ' in text or
            '譲渡益税徴収・還付のお知らせ' in text):
        return _parse(path,data,'notice',parse_notice)
    if any(marker in text for marker in
           ('取引残高報告書','お預り残高等の明細','STWHZB','FR00011M01')):
        return _parse(path,data,'balance',parse_balance)
    if '株式取引報告書' in text or '投資信託取引報告書' in text:
        return _parse(path,data,'trade',parse_report)
    raise _with_owner(ArchivedUnparsedDocument('対応する取引・配当・分割・残高報告の様式と確認できません。'),data)

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
