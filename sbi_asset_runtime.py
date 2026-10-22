"""Read-only current-asset tab recovery, local filters and rendering checks.

Only rendered SBI controls and a caller-retained, previously observed SBI URL
are used. Neither session URLs nor customer text are written to progress logs.
"""
import json
import re
import time
from urllib.parse import urlparse
from ledger import LedgerError
from login_credentials import CredentialError


def _trusted_url(value):
    try:
        url=urlparse(value)
        host=(url.hostname or '').lower()
        return url.scheme=='https' and not url.username and not url.password and (host=='sbisec.co.jp' or host.endswith('.sbisec.co.jp'))
    except (TypeError,ValueError):return False


def select_asset_page(page,owner=None,identity_verified=False,entry_url=None,progress=lambda _:None,cancelled=lambda:False,timeout=8):
    from sbi_views import frames,snapshot_dom,_check_cancelled
    from auto_login import trusted
    from post_identity import state
    try:context=page.context
    except AttributeError:
        page=page.page;context=page.context
    started=time.monotonic();deadline=started+timeout;restored=False;opened=None
    try:
        while True:
            _check_cancelled(cancelled)
            choices=[];seen_sbi=False
            for tab in list(context.pages):
                if tab.is_closed() or not trusted(tab):continue
                seen_sbi=True
                try:text,_=snapshot_dom(tab)
                except LedgerError:continue
                current=state(text,owner) if owner else 'NOT_RENDERED'
                if current=='MISMATCH':raise CredentialError('資産取得前のSBI画面に別口座の名義を検出しました。保存せず停止しました。')
                passwords=[]
                for frame in frames(tab):
                    if not trusted(frame):continue
                    loc=frame.locator('input[type="password"]')
                    passwords.extend(loc.nth(index) for index in range(loc.count()) if loc.nth(index).is_visible())
                if passwords or 'ログアウト' not in text:continue
                if not any(label in text for label in ('口座管理','口座サマリー','My資産','口座(円建)','口座（円建）')):continue
                if owner and not identity_verified and current!='MATCH':continue
                choices.append(tab)
            if choices:
                selected=page if page in choices else choices[-1]
                if selected is not page:progress('同じログイン状態のSBI画面へ戻り、資産取得を続けます。')
                return selected
            if not restored and entry_url is not None and (not seen_sbi or time.monotonic()-started>=.5):
                if not _trusted_url(entry_url):raise CredentialError('資産取得の戻り先が確認済みのSBI画面ではありません。')
                _check_cancelled(cancelled);restored=True
                opened=context.new_page()
                progress('ログイン時に確認したSBI画面を同じブラウザー内で開き直します。')
                opened.goto(entry_url,wait_until='domcontentloaded',timeout=15000)
                continue
            if time.monotonic()>=deadline:break
            live=[tab for tab in list(context.pages) if not tab.is_closed()]
            if not live:raise CredentialError('SBIのブラウザー画面が閉じられました。')
            live[-1].wait_for_timeout(250)
        raise CredentialError('資産取得に使うログイン済みSBI画面を確認できません。電子ポストとは別のSBI画面・ログイン状態を確認してください。')
    except Exception:
        # A failed restoration must not close the user's existing e-post tab.
        if opened is not None:
            try:opened.close()
            except Exception:pass
        raise


_FILTER_REFRESH=r'''el=>{
 for(let node=el.parentElement,depth=0;node&&depth<7;node=node.parentElement,depth++){
  const matches=Array.from(node.querySelectorAll('button,input[type=button],input[type=submit],[role=button]')).filter(button=>{
   const rect=button.getBoundingClientRect();
   const label=(button.innerText||button.value||button.getAttribute('aria-label')||'').replace(/\s+/g,'');
   return rect.width&&rect.height&&label==='更新'&&!button.disabled;
  });
  if(matches.length===1){const marker='cash-'+Date.now()+'-'+Math.random().toString(36).slice(2);matches[0].setAttribute('data-family-trades-cash-refresh',marker);return marker;}
  if(matches.length>1)return null;
  if(node.tagName==='MAIN'||node.tagName==='BODY')break;
 }
 return null;
}'''


def apply_cash_filter(page):
    from sbi_views import frames
    from auto_login import trusted
    changed=False
    for frame in frames(page):
        if not trusted(frame):continue
        loc=frame.get_by_role('checkbox',name='預り金を除外',exact=True)
        visible=[loc.nth(index) for index in range(loc.count()) if loc.nth(index).is_visible()]
        if len(visible)>1:raise LedgerError('預り金の表示切替が複数あり、一意に確認できません。')
        if not visible or not visible[0].is_checked():continue
        checkbox=visible[0]
        checkbox.uncheck(timeout=5000)
        marker=checkbox.evaluate(_FILTER_REFRESH)
        if marker:
            # This button was observed beside this exact display filter.
            frame.locator('[data-family-trades-cash-refresh="'+marker+'"]').click(timeout=5000)
        changed=True
    return changed


def current_view_busy(page):
    from auto_login import trusted
    from sbi_views import frames
    for frame in frames(page):
        if not trusted(frame):continue
        loc=frame.locator('[aria-busy="true"],[role="progressbar"]')
        if any(loc.nth(index).is_visible() for index in range(loc.count())):return True
    return False


def read_current_view(page,kind,owner=None,cancelled=lambda:False,timeout=7,stable_seconds=.6):
    from sbi_views import (snapshot_dom,parse_summary,parse_my_assets,parse_currency_summary,
                           _check_cancelled,_check_asset_identity,ObservationConflict)
    parser={'口座サマリー':parse_summary,'My資産':parse_my_assets,'口座(外貨建)':parse_currency_summary}[kind]
    deadline=time.monotonic()+timeout;last=None;stable_since=None;best=None;last_error=None
    while time.monotonic()<deadline:
        _check_cancelled(cancelled);_check_asset_identity(page,owner)
        if current_view_busy(page):
            last=None;stable_since=None;best=None;page.wait_for_timeout(200);continue
        text,tables=snapshot_dom(page)
        try:
            values=parser(text,tables)
            if kind=='口座サマリー' and not any(key in values for key in ('cash_total','growth_remaining','tsumitate_remaining')):
                raise LedgerError('口座サマリーの本体を確認できません。')
            signature=json.dumps(values,sort_keys=True,ensure_ascii=False)
            if signature!=last:last=signature;stable_since=time.monotonic();best=None
            elif time.monotonic()-stable_since>=stable_seconds:
                best=(text,tables,values)
                # The real legacy summary renders cash and NISA independently.
                # Keep waiting if its expected blocks are still incomplete.
                if kind!='口座サマリー' or all(key in values for key in ('cash_total','growth_remaining','tsumitate_remaining')):
                    return best
        except ObservationConflict as error:
            last_error=error;last=None;stable_since=None;best=None
        except LedgerError as error:
            last_error=error;last=None;stable_since=None;best=None
        page.wait_for_timeout(200)
    _check_cancelled(cancelled)
    if best is not None:return best
    if last_error is not None:raise last_error
    raise LedgerError('資産画面の金額表示が読み込み中、または安定した表示を確認できません。')
