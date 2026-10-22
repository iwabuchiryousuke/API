"""Store route evidence without session URLs, form values, cookies or OTP."""
from datetime import datetime
import json
from pathlib import Path
from urllib.parse import urlparse


def save_navigation_failure(context, directory, account, stage, progress=lambda _:None):
    from auto_login import trusted
    from sbi_navigation import scan, STEPS
    from sbi_views import norm, frames
    wanted = {norm(name) for names, _ in STEPS for name in names}
    evidence = []
    for page in list(context.pages):
        if page.is_closed():continue
        for frame in frames(page):
            if not trusted(frame):continue
            try:
                rows = scan(frame)
                relevant = []
                for row in rows:
                    labels = [str(label) for label in row['labels'] if norm(label) in wanted]
                    if labels:
                        relevant.append({'labels':labels, 'domestic':bool(row['domestic']),
                                         'visible':bool(row['visible']), 'enabled':bool(row['enabled'])})
                evidence.append({'host':urlparse(frame.url).hostname, 'controls_scanned':len(rows), 'candidates':relevant})
            except Exception:
                continue
    try:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f'navigation_{account}_{datetime.now():%Y%m%d_%H%M%S_%f}.json'
        path.write_text(json.dumps({'account':account, 'stage':stage, 'frames':evidence},ensure_ascii=False,indent=2),encoding='utf-8')
        progress('画面移動の診断を保存：'+path.name+'（セッションURL・認証情報なし）')
        return path
    except OSError:
        progress('画面移動の診断ファイルを保存できませんでした。')
        return None
