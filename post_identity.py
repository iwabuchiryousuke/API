"""Wait for electronic-post identity; distinguish missing DOM from a wrong account."""
import re,time,unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from login_credentials import CredentialError

def compact(value):
    return ''.join(c for c in unicodedata.normalize('NFKC',str(value)) if not c.isspace() and unicodedata.category(c)!='Cf')

def state(text,expected):
    from family_profile import OWNERS
    text=compact(text);expected=compact(expected)
    names=set(compact(n) for n in OWNERS.values())|{expected}
    present={n for n in names if n and n+'様' in text}
    if present=={expected}:return 'MATCH'
    if present:return 'MISMATCH'
    return 'NOT_RENDERED'

def verify(page,owner,timeout=30,progress=lambda message:None,clock=time.monotonic,cancelled=lambda:False):
    from epost_collector import is_post_url,POST_HOST
    if not compact(owner):raise CredentialError('PDF名義の設定が空です。保存していません。')
    deadline=clock()+timeout;last=None;counts={'frames':0,'read':0,'chars':0};attempt=0
    while True:
        if cancelled():raise CredentialError('中止しました。名義の表示待ちを終了しました。')
        if not page.url.startswith('https://') or not is_post_url(page.url):
            raise CredentialError('電子ポストのURLを確認できません。保存していません。')
        texts=[];counts={'frames':0,'read':0,'chars':0}
        # Include the top-level document and only same-host frames, never arbitrary embeds.
        candidates=[page]
        candidates+=list(getattr(page,'frames',[]) or [])
        for f in candidates:
            parsed=urlparse(f.url)
            if parsed.scheme!='https' or parsed.hostname!=POST_HOST:continue
            counts['frames']+=1
            try:
                text=f.locator('body').inner_text(timeout=1000)
                texts.append(text);counts['read']+=1;counts['chars']+=len(text)
            except Exception:continue
        attempt+=1;current=state('\n'.join(texts),owner)
        if current!=last:
            progress(f'電子ポスト名義判定={current} / 読取成功={counts["read"]} / 文字数={counts["chars"]}')
            last=current
        if current=='MATCH':return
        if current=='MISMATCH':
            raise CredentialError('電子ポストに選択口座以外の家族名義を検出しました。ログイン口座と選択口座を確認してください。保存していません。')
        if clock()>=deadline:
            progress(f'名義表示待ち終了 / 試行={attempt} / 対象文書={counts["frames"]} / 読取成功={counts["read"]} / 文字数={counts["chars"]}')
            raise CredentialError('電子ポストの名義表示を待ちましたが確認できません。別名義とは断定していません。診断用画像で画面を確認してください。保存していません。')
        page.wait_for_timeout(400)

def save_failure_evidence(page,folder,account,progress):
    """Local screenshot only; no cookies, credentials, OTP, or full DOM logging."""
    from epost_collector import is_post_url
    if not page.url.startswith('https://') or not is_post_url(page.url):return
    try:
        folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
        target=folder/f'post_failure_{account}_{datetime.now():%Y%m%d_%H%M%S}.png'
        page.screenshot(path=str(target),full_page=False,timeout=5000)
        progress('電子ポスト停止時の画面を保存：'+target.name+'（口座情報を含むため共有前に確認してください）')
    except Exception:progress('電子ポスト停止時の画像は保存できませんでした。')
