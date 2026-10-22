"""Sequential A-D collection, with explicit fail-closed screen matching.
This is an integration implementation; live SBI UI validation is still required.
"""
import re,time
from pathlib import Path
from urllib.parse import urlparse
from ledger import Store
from gmail_auth import service,wait_code,recent_ids,wait_message,extract_auth_link
from epost_collector import BASE,autofill_login,_login_page,wait_for_post_tab,collect
from login_credentials import CredentialError

def trusted(page):
    host=(urlparse(page.url).hostname or '').lower()
    return urlparse(page.url).scheme=='https' and (host=='sbisec.co.jp' or host.endswith('.sbisec.co.jp'))
def unique_click(page,role,names):
    if not trusted(page):raise CredentialError('SBIの画面であることを確認できません。')
    choices=[]
    for name in names:
        loc=page.get_by_role(role,name=name,exact=True)
        for i in range(loc.count()):
            if loc.nth(i).is_visible():choices.append(loc.nth(i))
    if len(choices)!=1:raise CredentialError('操作ボタンを一意に特定できません。手動収集を利用してください。')
    choices[0].click(timeout=10000)

def submit_login(page):
    """The submit control in the visible password form, not a header nav link."""
    if not trusted(page):raise CredentialError('ログイン送信先を確認できません。')
    passwords=page.locator('input[type="password"]')
    visible_passwords=[passwords.nth(i) for i in range(passwords.count()) if passwords.nth(i).is_visible()]
    if len(visible_passwords)!=1:raise CredentialError('ログインパスワード欄を一意に確認できません。')
    form=visible_passwords[0].locator('xpath=ancestor::form[1]')
    if form.count()==1:
        buttons=form.locator('button, input[type="submit"], input[type="image"]')
        matches=[]
        for i in range(buttons.count()):
            b=buttons.nth(i)
            label=' '.join([b.inner_text() or '',b.get_attribute('value') or '',b.get_attribute('alt') or '',b.get_attribute('aria-label') or ''])
            if b.is_visible() and re.search(r'ログイン|LOGIN|Login',label):matches.append(b)
        if len(matches)==1:matches[0].click(timeout=10000);return
    unique_click(page,'button',['ログイン'])

def otp_fields(page):
    if not trusted(page):return []
    labeled=page.get_by_role('textbox',name=re.compile('認証コード|認証番号|ワンタイムパスワード'))
    candidates=[labeled.nth(i) for i in range(labeled.count()) if labeled.nth(i).is_visible()]
    if len(candidates)==1:return candidates
    if not candidates:
        fields=page.locator('input[autocomplete="one-time-code"]')
        candidates=[fields.nth(i) for i in range(fields.count()) if fields.nth(i).is_visible()]
    if len(candidates)==1:return candidates
    if 4<=len(candidates)<=12 and all(f.get_attribute('maxlength')=='1' for f in candidates):return candidates
    return []

def otp_length(fields):
    if len(fields)>1:return len(fields)
    if not fields:return None
    raw=fields[0].get_attribute('maxlength')
    if raw and str(raw).isdigit() and 4<=int(raw)<=12:return int(raw)
    return None

def fill_otp(fields,code):
    if len(fields)==1:fields[0].fill(code)
    elif 4<=len(fields)<=12 and len(code)==len(fields):
        for f,d in zip(fields,code):f.fill(d)
    else:raise CredentialError('認証コード入力欄を特定できません。')

def auth_label(value):
    # Visible SBI label may contain spacing or an arrow icon's text.
    import unicodedata
    value=re.sub(r'\s+','',unicodedata.normalize('NFKC',value or ''))
    return value.rstrip('>›»❯→▶')

def submit_otp(page,fields,progress=lambda message:None,timeout=10,cancelled=lambda:False):
    """Submit only the observed authentication action; never resend or guess."""
    if cancelled():raise CredentialError('中止しました。認証送信は行いません。')
    if not trusted(page) or not fields:raise CredentialError('認証入力先を確認できません。')
    # Blurring permits client-side input validation to enable the control.
    fields[-1].press('Tab')
    end=time.monotonic()+timeout;count=0;enabled=False
    while True:
        if cancelled():raise CredentialError('中止しました。認証送信は行いません。')
        if not trusted(page):raise CredentialError('認証入力先が変わりました。')
        form=fields[0].locator('xpath=ancestor::form[1]')
        scope=form if form.count()==1 else page
        controls=scope.locator('button, input[type="submit"], input[type="button"], input[type="image"], a, [role="button"]')
        matches=[]
        for i in range(controls.count()):
            item=controls.nth(i)
            if not item.is_visible():continue
            labels=[item.inner_text(),item.get_attribute('value'),item.get_attribute('aria-label'),item.get_attribute('alt')]
            if any(auth_label(label)=='認証する' for label in labels):matches.append(item)
        count=len(matches);enabled=count==1 and matches[0].is_enabled() and matches[0].get_attribute('aria-disabled')!='true'
        if count>1:raise CredentialError('認証送信：表示中の「認証する」が複数あります。誤操作防止のため停止しました。')
        if enabled:
            progress('認証送信ボタンを確認しました。')
            matches[0].click(timeout=10000)
            progress('認証送信ボタンを押しました。')
            return
        if time.monotonic()>=end:break
        (page.page if hasattr(page,'page') else page).wait_for_timeout(250)
    raise CredentialError(f'認証送信：「認証する」の表示候補={count}、有効={enabled}。入力欄のエラー表示とボタンを確認してください。')

def visible(page,role,names):
    if not trusted(page):return []
    result=[]
    for name in names:
        loc=page.get_by_role(role,name=name,exact=True)
        result.extend(loc.nth(i) for i in range(loc.count()) if loc.nth(i).is_visible())
    return result

def device_screen_code(text):
    codes=set(re.findall(r'認証コード[ \t:：]*\n?\s*(\d{6})(?!\d)',text))
    if len(codes)!=1:raise CredentialError('画面のデバイス認証コードを特定できません。手動確認が必要です。')
    return codes.pop()

def device_auth(page,context,api,recipient,progress=lambda _:None,cancelled=lambda:False):
    if cancelled():raise CredentialError('中止しました。')
    seen=recent_ids(api,time.time());started=time.time()
    unique_click(page,'button',['Eメールを送信する'])
    _,url=wait_message(api,recipient,started,extract_auth_link,seen,progress=progress,cancelled=cancelled)
    if cancelled():raise CredentialError('中止しました。')
    auth=context.new_page()
    try:
        auth.goto(url,wait_until='domcontentloaded');del url
        if not trusted(auth) or urlparse(auth.url).hostname!='m.sbisec.co.jp':raise CredentialError('認証リンクの移動先が一致しません。')
        acknowledgement='ログイン画面はメールやSMSなどから開いていません'
        if cancelled():raise CredentialError('中止しました。')
        if visible(auth,'button',[acknowledgement]):unique_click(auth,'button',[acknowledgement])
        # Code comes from the original SBI page, NOT from the email.
        code=device_screen_code(page.locator('body').inner_text())
        field=auth.get_by_role('textbox',name=re.compile('認証コード|認証番号'))
        if field.count()!=1:raise CredentialError('認証コード入力欄を一意に確認できません。')
        field.fill(code);unique_click(auth,'button',['認証する']);del code
        auth.get_by_text('認証コードを確認しました',exact=False).wait_for(timeout=15000)
        if cancelled():raise CredentialError('中止しました。')
        check=page.get_by_role('checkbox')
        candidates=[check.nth(i) for i in range(check.count()) if check.nth(i).is_visible()]
        if len(candidates)!=1:raise CredentialError('デバイス登録の確認欄を一意に確認できません。')
        candidates[0].check();unique_click(page,'button',['デバイスを登録する'])
    finally:auth.close()

def authenticated_page(context,owner,timeout=30,cancelled=lambda:False):
    from post_identity import state
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if cancelled():raise CredentialError('中止しました。')
        for p in context.pages:
            if p.is_closed():continue
            for f in p.frames:
                if not trusted(f):continue
                try:body=f.locator('body').inner_text(timeout=1500)
                except Exception:continue
                identity=state(body,owner)
                if identity=='MISMATCH':raise CredentialError('選択口座と異なるログイン名義を検出したため停止しました。')
                if identity=='MATCH' and 'ログアウト' in body:return p
        time.sleep(.5)
    raise CredentialError('ログイン後の口座名義を確認できません。別口座への保存を防ぐため停止しました。')

def open_electronic_post(page,context,progress=lambda _:None,cancelled=lambda:False,manual_seconds=180):
    from sbi_navigation import open_post_resilient
    return open_post_resilient(page,context,progress=progress,cancelled=cancelled,manual_seconds=manual_seconds)

def run_one(playwright,account,asset_store=None,gmail_session=None,progress=lambda message:None,cancelled=lambda:False,documents=True,capture_assets=True,target_dates=()):
    assets_observed=False
    if account not in tuple('ABCD'):raise CredentialError('SBIの自動取得は口座A～Dです。')
    if not documents and not capture_assets:raise CredentialError('取得する内容が選択されていません。')
    if cancelled():raise CredentialError('中止しました。')
    api,recipient=gmail_session or service(BASE/'data',account)
    store=asset_store or Store(BASE/'data')
    try:owner=next(a for a in store.accounts() if a['id']==account).get('owner_name','')
    finally:
        if asset_store is None:store.close()
    if not owner:raise CredentialError('最初に各口座のPDF名義を設定してください。')
    stage='Edgeの起動'
    progress(f'口座{account}：{stage}')
    try:browser=playwright.chromium.launch(channel='msedge',headless=False)
    except Exception:raise CredentialError(f'口座{account}：Edgeを起動できません。Edgeとsetup_all_windows.batの実行を確認してください。') from None
    try:
        context=browser.new_context(accept_downloads=True);page=context.new_page();page.goto('https://www.sbisec.co.jp/',wait_until='domcontentloaded')
        stage='SBIログイン画面の確認';progress(f'口座{account}：{stage}')
        if not autofill_login(page,context,account,automatic=True):
            from epost_collector import login_diagnostics
            raise CredentialError('ログイン欄の自動入力に失敗しました。'+login_diagnostics(context))
        page=_login_page(context)
        if page is None:raise CredentialError('ログイン画面がありません。')
        stage='SBIログイン・メール認証';progress(f'口座{account}：{stage}')
        seen=recent_ids(api,time.time());started=time.time();submit_login(page)
        page=page.page if hasattr(page,'page') else page
        deadline=time.monotonic()+35;otp=None;device_done=False;logged_in=False
        while time.monotonic()<deadline:
            if cancelled():raise CredentialError('中止しました。')
            for p in [f for tab in context.pages for f in tab.frames]:
                if not trusted(p):continue
                try:body=p.locator('body').inner_text(timeout=3000)
                except Exception:continue
                if visible(p,'button',['Eメールを送信する']) and 'デバイス認証' in body:
                    device_auth(p,context,api,recipient,progress=progress,cancelled=cancelled);page=p;device_done=True;break
                if any(x in body for x in ('ロボットでは','CAPTCHA','アクセスが制限')):raise CredentialError('追加確認・アクセス制限があります。自動処理を停止しました。')
                fields=otp_fields(p)
                if fields:page=p;otp=fields;break
                if re.sub(r'\s+','',owner) in re.sub(r'\s+','',body) and 'ログアウト' in body:page=p;logged_in=True;break
            if otp or device_done or logged_in:break
            (page.page if hasattr(page,'page') else page).wait_for_timeout(500)
        if not (otp or device_done or logged_in):raise CredentialError('ログイン後の画面を認識できません。認証画面・エラー表示のスクリーンショットで確認が必要です。')
        if otp:
            length=otp_length(otp)
            progress(f'口座{account}：入力欄のコード長='+str(length or '指定なし・6文字で照合'))
            _,code=wait_code(api,recipient,started,seen,length=length,progress=lambda msg:progress(f'口座{account}：{msg}'),cancelled=cancelled)
            if not trusted(page):raise CredentialError('認証入力先が変わりました。')
            stage='認証コード入力';progress(f'口座{account}：{stage}')
            fill_otp(otp,code);del code
            stage='認証送信ボタン';progress(f'口座{account}：{stage}')
            submit_otp(page,otp,progress=lambda msg:progress(f'口座{account}：{msg}'),cancelled=cancelled)
            (page.page if hasattr(page,'page') else page).wait_for_timeout(2000)
        stage='ログイン完了・名義確認';progress(f'口座{account}：{stage}')
        from sbi_views import landing,verify_post_owner,capture_account,select_asset_page
        page=landing(context,cancelled=cancelled)
        if not documents:page=authenticated_page(context,owner,cancelled=cancelled)
        # A post navigation may replace this very Page. Keep its observed SBI
        # entry only in memory so we can return within the same login context.
        asset_entry_url=page.url if trusted(page) else None
        if cancelled():raise CredentialError('中止しました。')
        saved=duplicate=failed=0
        pdf_error=None
        if documents:
            stage='電子ポストへ移動・名義確認';progress(f'口座{account}：{stage}')
            try:
                post=open_electronic_post(page,context,progress=progress,cancelled=cancelled,manual_seconds=0 if capture_assets else 180)
            except CredentialError:
                if cancelled() or not capture_assets:raise
                # No PDF owner check succeeded. Asset fallback therefore needs
                # an independent positive SBI owner check in this same context.
                page=authenticated_page(context,owner,timeout=5,cancelled=cancelled)
                pdf_error='電子ポストへの自動移動を完了できませんでした。'
            else:
                # A mismatch here never becomes a recoverable navigation error.
                verify_post_owner(post,owner,progress=lambda msg:progress(f'口座{account}：{msg}'),cancelled=cancelled)
                progress(f'口座{account}：電子ポストの名義一致を確認しました。')
                stage='PDFの保存';progress(f'口座{account}：{stage}')
                try:
                    saved,duplicate,failed=collect(post,context,account,BASE/'data'/'電子ポストPDF',progress=progress,cancelled=cancelled)
                    progress(f'口座{account}：PDF 新規{saved}・保存済み{duplicate}・失敗{failed}。')
                    if failed:pdf_error='一部書類の収集を完了できませんでした。'
                except Exception as error:
                    if cancelled():raise CredentialError('中止しました。')
                    pdf_error=str(error) if isinstance(error,CredentialError) else 'PDF一覧・保存の処理を完了できませんでした。取得結果を確認してください。'
            if pdf_error and capture_assets:progress(f'口座{account}：{pdf_error} 資産の取得は続けます。')
        if not capture_assets:
            if pdf_error:raise CredentialError(pdf_error+' 保存済みPDFは保持しています。台帳への反映は行っていません。')
            return {'complete':True,'pdf_collected':documents,'assets_observed':False,'saved':saved,'duplicate':duplicate}
        # Preserve PDFs before navigating unrelated asset pages.
        stage='口座サマリー・My資産の取得';progress(f'口座{account}：{stage}')
        store=asset_store or Store(BASE/'data')
        from asset_snapshots import KEYS,HistoricalConflict
        status={'available':[],'missing':list(KEYS)+['growth_remaining','tsumitate_remaining'],'usd_cash_observed':False}
        from sbi_views import _jst_now
        from acquisition_journal import persist_capture,JournalError
        capture_started=_jst_now().isoformat()
        journal_saved=False
        try:
            page=select_asset_page(page,owner=owner,identity_verified=True,entry_url=asset_entry_url,progress=progress,cancelled=cancelled)
            assets_observed=capture_account(page,store,account,progress,owner=owner,cancelled=cancelled,identity_verified=True,diagnostics=BASE/'data'/'diagnostics',target_dates=target_dates)
            from sbi_views import capture_status
            status=capture_status(store,account,since=capture_started)
            # Current coverage and exact-date coverage are reported separately.
            # A missing historical field must not mislabel verified current data.
            assets_observed=status.get('complete',assets_observed)
            if cancelled():raise CredentialError('中止しました。')
            stage='資産取得証跡の保存'
            try:journal=persist_capture(BASE,account,owner,store,capture_started,target_dates=target_dates)
            except (JournalError,OSError):raise CredentialError('取得済み資産の証跡を保存できません。空き容量・保存先を確認してください。台帳への一括反映は停止しました。') from None
            journal_saved=bool(journal)
            if journal_saved:progress(f'口座{account}：確認できた資産の取得証跡を保存しました。一括反映の確認後に台帳へ登録します。')
        except CredentialError:
            raise
        except JournalError:
            raise CredentialError('取得済み資産の証跡を保存できません。空き容量と診断を確認してください。台帳への一括反映は停止しました。') from None
        except HistoricalConflict:
            raise CredentialError('指定日の資産表示と確認済み記録が一致しません。既存値を保持し、今回の一括反映を停止しました。診断とSBIの表示基準日を確認してください。') from None
        except Exception:
            if cancelled():raise CredentialError('中止しました。')
            assets_observed=False
            progress(f'口座{account}：資産額は未取得です。保存済みPDFの取り込みは続行します。')
        finally:
            if asset_store is None:store.close()
        if cancelled():raise CredentialError('中止しました。')
        if pdf_error:
            stage='PDF取得の完了確認'
            retained='保存済みPDFと確認できた資産の取得証跡は保持しています。' if journal_saved else '保存済みPDFは保持しています。資産の未取得項目は診断で確認してください。'
            raise CredentialError(pdf_error+' '+retained+'台帳への一括反映は未完了です。')
        if not documents and not status['available']:raise CredentialError('資産情報を確認できませんでした。資産画面の診断を確認してください。')
        missing=list(status['missing'])
        if not status['usd_cash_observed']:missing.append('usd_cash')
        return {'complete':True,'assets_observed':assets_observed,'assets_missing':missing,
                'usd_cash_observed':status['usd_cash_observed'],'pdf_collected':documents,'saved':saved,'duplicate':duplicate}
    except CredentialError as e:
        if 'post' in locals():
            from post_identity import save_failure_evidence
            save_failure_evidence(post,BASE/'data'/'diagnostics',account,progress)
        elif 'context' in locals() and stage=='電子ポストへ移動・名義確認':
            from navigation_diagnostics import save_navigation_failure
            try:save_navigation_failure(context,BASE/'data'/'diagnostics',account,stage,progress)
            except Exception:progress('画面移動の診断は保存できませんでした。')
        raise CredentialError(f'口座{account}／{stage}：{e}') from None
    except Exception:raise CredentialError(f'口座{account}／{stage}で停止しました。画面の読み込み・追加認証・接続状況を確認してください。') from None
    finally:browser.close()

def main():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        for account in 'ABCD':
            print(f'口座{account}を開始します。')
            run_one(pw,account)
if __name__=='__main__':
    try:main()
    except CredentialError as e:print('停止：'+str(e))
    except Exception:print('自動処理を停止しました。接続・Gmail設定・SBI画面を確認し、手動収集をご利用ください。')
    input('Enterで閉じます：')
