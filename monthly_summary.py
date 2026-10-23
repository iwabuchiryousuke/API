"""Read-only monthly measurements from the legacy SBI account summary.

Stocks here are the screen's neutral '株式' valuation, not a reconstructed
国内株式等 total. Acquisition time is retained separately from any page clock.
"""
import json
import math
import re
import time
import unicodedata
from datetime import datetime
from decimal import Decimal

from ledger import LedgerError
from login_credentials import CredentialError
from sbi_views import ObservationConflict

REQUIRED=('summary_stocks','cash_total')
_LABELS={
    '現金残高等(合計)':'cash_total',
    '現金残高等合計':'cash_total',
    'SBI証券口座分':'broker_cash',
    'スィープ専用銀行口座':'bank',
    'スイープ専用銀行口座':'bank',
    '株式':'summary_stocks',
    '株式(円)':'summary_stocks',
    '株式評価額':'summary_stocks',
    '計':'summary_total',
    '合計':'summary_total',
    '評価額合計':'summary_total',
}
_BOUNDARIES={
    'ポートフォリオ','買付余力','ポイント数','NISA投資可能枠',
    'ご注意事項','ヘルプ','口座サマリー','取引履歴','注文照会',
}
_OTHER_ASSETS={
    '投資信託','投信','債券','円貨建債券','外貨建債券','外国株式',
    '外貨建MMF','MMF','CW','現金以外の資産','その他',
}


def _compact(value):
    return re.sub(r'\s+','',unicodedata.normalize('NFKC',str(value)))


def _heading(value):
    text=_compact(value).lstrip('|｜')
    if text.endswith('詳細'):text=text[:-2]
    return text.rstrip('>›»❯→▶↗')


def _amount(value):
    from asset_snapshots import money
    result=money(value,allow_bare=True)
    return result if result is not None and math.isfinite(result) else None


def _assign(values,key,value):
    if value is None:return
    if key in values and values[key]!=value:
        raise ObservationConflict('口座サマリーの同名評価額に異なる表示があります。')
    values[key]=value


def _labelled_line(line,next_line=''):
    compact=_compact(line)
    for label in sorted(_LABELS,key=len,reverse=True):
        if compact==label:return _LABELS[label],_amount(next_line)
        if compact.startswith(label):
            value=_amount(compact[len(label):])
            if value is not None:return _LABELS[label],value
    return None,None


def _boundary(value):
    name=_heading(value)
    return name in _BOUNDARIES or name.startswith(('ポートフォリオ','株式(現物','保有株数','取得単価'))


def _text_sections(text):
    lines=[line.strip() for line in unicodedata.normalize('NFKC',text).splitlines() if line.strip()]
    sections=[]
    for start,line in enumerate(lines):
        if _heading(line)!='保有資産評価':continue
        section=[]
        for item in lines[start+1:]:
            if _boundary(item) or _heading(item)=='保有資産評価':break
            section.append(item)
            # The displayed total terminates this legacy section. A later
            # portfolio quantity must not become another '株式' valuation.
            if len(section)>=2:
                key,value=_labelled_line(section[-2],section[-1])
                if key=='summary_total' and value is not None:break
            key,value=_labelled_line(item)
            if key=='summary_total' and value is not None:break
        sections.append(section)
    return sections


def _table_sections(tables):
    sections=[]
    for table in tables:
        current=None
        for row in table:
            # Only an exact heading cell proves scope. Layout ancestors whose
            # cells contain the whole page are deliberately not accepted.
            if any(_heading(cell)=='保有資産評価' for cell in row):
                if current is not None:sections.append(current)
                current=[];continue
            if current is None:continue
            if any(_boundary(cell) for cell in row):break
            current.append(row)
            if len(row)==2 and _LABELS.get(_compact(row[0]))=='summary_total' and _amount(row[1]) is not None:break
        if current is not None:sections.append(current)
    return sections


def _read_section(lines=(),rows=()):
    values={};other=False
    cells=list(lines)+[cell for row in rows for cell in row]
    for index,cell in enumerate(cells):
        match=re.search(r'(?:^|\()単位[:(]?([^()]*)',_compact(cell))
        if not match:continue
        unit=match.group(1) or (_compact(cells[index+1]) if index+1<len(cells) else '')
        if unit.upper() not in ('円','JPY','日本円'):
            raise ObservationConflict('保有資産評価に未対応の単位が表示されています。円の評価額として保存しません。')
    for index,line in enumerate(lines):
        following=lines[index+1] if index+1<len(lines) else ''
        key,value=_labelled_line(line,following)
        if key:_assign(values,key,value)
        compact=_compact(line)
        if any(compact==label or (compact.startswith(label) and _amount(compact[len(label):]) is not None) for label in _OTHER_ASSETS):other=True
        cells=[cell.strip() for cell in line.split('\t')]
        if len(cells)==2 and _compact(cells[0]) not in _LABELS and _amount(cells[1]) is not None:other=True
        # Unrecognised numeric rows may represent an additional asset class;
        # they prevent a false assertion that cash+stocks is the entire total.
        if key is None and _amount(following) is not None and _amount(line) is None and compact not in ('詳細','円','単位:円','単位(円)'):
            other=True
    for row in rows:
        if len(row)!=2:continue
        label=_compact(row[0]);key=_LABELS.get(label)
        if key:_assign(values,key,_amount(row[1]))
        elif label in _OTHER_ASSETS or (_amount(row[1]) is not None and label not in ('詳細','円','単位:円','単位(円)')):
            other=True
    return values,other


def _nisa_values(text,tables):
    from sbi_views import parse_summary
    lines=[line.strip() for line in unicodedata.normalize('NFKC',text).splitlines() if line.strip()]
    selected=[];selected_tables=[]
    pattern=re.compile(r'^投資可能枠\(\d{4}年\)[−ー―—-]*NISA\((?:成長投資枠|つみたて投資枠)\)')
    for index,line in enumerate(lines):
        if pattern.match(_compact(line)):
            selected.append(line)
            if index+1<len(lines):selected.append(lines[index+1])
    for table in tables:
        rows=[row for row in table if len(row)==2 and pattern.match(_compact(row[0]))]
        if rows:selected_tables.append(rows)
    if not selected and not selected_tables:return {}
    try:parsed=parse_summary('\n'.join(selected),selected_tables)
    except ObservationConflict:raise
    except LedgerError:return {}
    return {key:parsed[key] for key in ('growth_remaining','tsumitate_remaining') if key in parsed}


def parse_monthly_summary(text,tables=()):
    """Parse visible, explicitly scoped 保有資産評価 and optional NISA rows.

    Missing balances remain absent; no amount or bank balance is reconstructed
    by subtraction. Bare numbers are JPY only within this observed legacy
    yen-account valuation block, never a portfolio or buying-power section.
    """
    values={};other=False
    scoped_tables=_table_sections(tables)
    # Browser innerText flattens adjacent tables. A header-only table must not
    # lend its scope to an unrelated portfolio table later in body text.
    # When table structure proves the heading, it is the valuation boundary;
    # use text only for pages with no structurally headed table at all.
    for lines in (() if scoped_tables else _text_sections(text)):
        section,has_other=_read_section(lines=lines);other|=has_other
        for key,value in section.items():_assign(values,key,value)
    for rows in scoped_tables:
        section,has_other=_read_section(rows=rows);other|=has_other
        for key,value in section.items():_assign(values,key,value)
    if all(key in values for key in ('cash_total','broker_cash','bank')):
        if Decimal(str(values['cash_total']))!=Decimal(str(values['broker_cash']))+Decimal(str(values['bank'])):
            raise ObservationConflict('口座サマリーの現金合計と内訳が一致しません。')
    if all(key in values for key in ('summary_stocks','cash_total','summary_total')):
        total=Decimal(str(values['summary_total']))
        known=Decimal(str(values['summary_stocks']))+Decimal(str(values['cash_total']))
        if total<known or (not other and total!=known):
            raise ObservationConflict('保有資産評価の計と株式・現金の表示が一致しません。')
    values.update(_nisa_values(text,tables))
    return values


_VISIBLE_TABLES=r'''els=>{const visible=e=>Array.from(e.getClientRects()).some(r=>r.width>0&&r.height>0)&&getComputedStyle(e).visibility==='visible';
 return els.filter(visible).map(t=>Array.from(t.rows).filter(visible).map(r=>Array.from(r.cells).filter(visible).map(c=>c.innerText)))}'''


def _snapshot(page):
    from auto_login import trusted
    from sbi_views import frames
    chunks=[]
    for frame in frames(page):
        if not trusted(frame):continue
        try:
            embedded=frame;visible=True
            while getattr(embedded,'parent_frame',None) is not None:
                if not embedded.frame_element().is_visible():visible=False;break
                embedded=embedded.parent_frame
            if not visible:continue
            text=frame.locator('body').inner_text(timeout=3000)
            tables=frame.locator('table').evaluate_all(_VISIBLE_TABLES)
            chunks.append((text,tables))
        except Exception:continue
    if not chunks:raise LedgerError('口座サマリーの本文を読み取れません。')
    return chunks


def _read_values(page):
    values={};texts=[]
    for text,tables in _snapshot(page):
        texts.append(text)
        # Scope cannot begin in one frame and continue in a different frame.
        for key,value in parse_monthly_summary(text,tables).items():_assign(values,key,value)
    return '\n'.join(texts),values


def _page_clock(text):
    """Retain an unambiguous displayed clock, without valuation-date claims."""
    stamps=set()
    pattern=r'(?<!\d)(\d{4})([-/])(\d{1,2})\2(\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?(?![\d:])'
    for match in re.finditer(pattern,text):
        year,_,month,day,hour,minute,second=match.groups()
        try:stamp=datetime(int(year),int(month),int(day),int(hour),int(minute),int(second or 0))
        except ValueError:return None
        stamps.add(stamp.strftime('%Y/%m/%d %H:%M:%S' if second is not None else '%Y/%m/%d %H:%M'))
    return next(iter(stamps)) if len(stamps)==1 else None


def _read_stable(page,owner,cancelled,timeout=7,stable_seconds=.6):
    from sbi_views import _check_asset_identity,_check_cancelled
    from sbi_asset_runtime import current_view_busy
    deadline=time.monotonic()+timeout;last=None;stable_since=None;best=None;last_error=None
    while time.monotonic()<deadline:
        _check_cancelled(cancelled);_check_asset_identity(page,owner)
        if current_view_busy(page):
            last=None;stable_since=None;best=None;page.wait_for_timeout(200);continue
        try:
            text,values=_read_values(page)
            signature=json.dumps(values,sort_keys=True,ensure_ascii=False)
            if signature!=last:last=signature;stable_since=time.monotonic();best=None
            elif values and time.monotonic()-stable_since>=stable_seconds:
                best=(text,values)
                if all(values.get(key) is not None for key in REQUIRED):return best
        except ObservationConflict as error:
            last_error=error;last=None;stable_since=None;best=None
        except LedgerError as error:
            last_error=error;last=None;stable_since=None;best=None
        page.wait_for_timeout(200)
    _check_cancelled(cancelled)
    if best is not None:return best
    if last_error is not None:raise last_error
    return '',{}


def capture_summary(page,store,account,owner=None,identity_verified=False,progress=lambda _:None,cancelled=lambda:False):
    """Measure stocks and cash from the summary only; no PDFs or My資産."""
    from sbi_views import (select_asset_page,navigate,_check_asset_identity,
                           _check_cancelled,save_observation,_jst_now)
    if account not in ('A','B','C','D'):raise LedgerError('口座サマリーの自動取得はA～Dです。')
    _check_cancelled(cancelled)
    page=select_asset_page(page,owner=owner,identity_verified=identity_verified,progress=progress,cancelled=cancelled)
    _check_asset_identity(page,owner,require_match=bool(owner) and not identity_verified)
    chunks=_snapshot(page)
    if not any(_text_sections(text) or _table_sections(tables) for text,tables in chunks):
        try:page=navigate(page,['口座管理'],timeout=5,cancelled=cancelled)
        except CredentialError:_check_cancelled(cancelled)
        chunks=_snapshot(page)
        if not any(_text_sections(text) or _table_sections(tables) for text,tables in chunks):
            try:page=navigate(page,['口座(円建)','口座サマリー'],timeout=5,cancelled=cancelled)
            except CredentialError:_check_cancelled(cancelled)
    text,values=_read_stable(page,owner,cancelled)
    _check_cancelled(cancelled);_check_asset_identity(page,owner)
    payload=save_observation(store,account,'口座サマリー',_page_clock(text) or '',values) if values else {'observed_at':_jst_now().isoformat(),'displayed_at':None}
    missing=[key for key in REQUIRED if values.get(key) is None]
    if missing:progress(f'口座{account}：口座サマリーは一部未取得です。未確認値は0で補いません。')
    else:progress(f'口座{account}：口座サマリーの株式評価額・現金を取得しました。')
    return {'complete':not missing,'missing':missing,'values':values,
            'observed_at':payload['observed_at'],'displayed_at':payload.get('displayed_at')}
