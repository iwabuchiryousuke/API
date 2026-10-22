"""Tk/main-thread UI and isolated background update worker."""
import threading,queue,os
from tkinter import ttk,messagebox
import tkinter as tk
from update_workflow import Workflow,Cancelled
from version import APP_VERSION

PHASES={'ACCOUNT_FAILED':'この口座の収集失敗・次の口座へ','AUTHORIZING':'Gmail連携を確認中','COLLECTING':'SBIから収集中','CHECKING_PDF':'PDFを検査中','PREPARING_EXCEL':'Excelを作成中','COMMITTING':'確認済みデータを登録中','DATA_COMMITTED':'登録完了・Excelを配置中','COMPLETED':'完了','COMPLETED_WITH_WARNINGS':'完了（未取得項目あり）','FAILED':'停止','CANCELLED':'中止','OUTPUT_PENDING':'登録完了・出力待ち'}

def launch(app,base,online=False,accounts=None,assets_only=False):
    if app.busy:return
    month=app.month.get().strip()
    try:app.store.records(month)
    except Exception as e:messagebox.showerror('対象月を確認',str(e));return
    if online:
        from initial_setup import ready,ensure_personal
        try:
            if not ready(base):ensure_personal(base,app.root)
            if not ready(base):return
        except Exception:
            messagebox.showerror('初回設定','初回設定を完了できません。setup_all_windows.batを実行し、設定キーを選択してください。');return
        from login_credentials import has_credentials
        from secure_settings import path
        missing=[]
        for a in app.store.accounts():
            if a['id']=='E' or (accounts and a['id'] not in accounts):continue
            if not a.get('owner_name'):missing.append(a['id']+'：PDF名義')
            if not has_credentials(base/'data',a['id']):missing.append(a['id']+'：ログイン情報')

        if missing:messagebox.showerror('初回設定が必要です','初回設定キーを登録してください。アプリを再起動すると初回設定を開きます。\n'+'\n'.join(missing));return
    app.busy=True
    for b in app.buttons:b.configure(state='disabled')
    dialog=tk.Toplevel(app.root);dialog.title('資産の自動取得' if assets_only else 'まとめて更新');dialog.geometry('880x580');dialog.transient(app.root)
    status=tk.StringVar(value='開始しています');ttk.Label(dialog,textvariable=status,padding=12).pack(anchor='w')
    details=tk.Text(dialog,wrap='word',height=24);details.pack(fill='both',expand=True,padx=12)
    actions=ttk.Frame(dialog);actions.pack(pady=12)
    events=queue.Queue();cancel=threading.Event();decision=threading.Event();choice=[False]
    diagnostics=[]
    def remember(message):
        diagnostics.append(str(message))
    def write_diagnostics():
        from datetime import datetime
        folder=base/'data'/'diagnostics';folder.mkdir(parents=True,exist_ok=True)
        file=folder/('auto_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'.txt')
        file.write_text(f'v{APP_VERSION} 自動取得診断（画面・メール本文・認証コードなし）\n'+'\n'.join(diagnostics),encoding='utf-8')
        return file
    def stop():cancel.set();decision.set();status.set('中止要求を受け付けました。現在の処理が戻るまでお待ちください。')
    cancel_button=ttk.Button(actions,text='中止',command=stop);cancel_button.pack(side='left',padx=8)
    dialog.protocol('WM_DELETE_WINDOW',stop)
    def review(summary,proposal,prepared):
        events.put(('review',(summary,proposal,prepared)))
        while not decision.wait(.2):
            if cancel.is_set():return False
        return choice[0] and not cancel.is_set()
    def worker():
        from login_credentials import CredentialError
        try:
            flow=Workflow(base,progress=lambda **x:events.put(('progress',x)),cancelled=cancel.is_set)
            if online:
                from playwright.sync_api import sync_playwright
                from auto_login import run_one
                from gmail_auth import ensure_service
                sessions={};auth_errors=[]
                for account in (accounts or 'ABCD'):
                    if account=='E':continue
                    if cancel.is_set():raise Cancelled()
                    events.put(('progress',{'phase':'AUTHORIZING','account':account}))
                    try:sessions[account]=ensure_service(base/'data',account,lambda msg:events.put(('message',msg)))
                    except CredentialError as e:auth_errors.append(f'口座{account}：{e}')
                if auth_errors:raise CredentialError('Gmail連携を完了できませんでした。\n'+'\n'.join(auth_errors))
                with sync_playwright() as pw:result=flow.run(month,collect=lambda a,s:run_one(pw,a,s,gmail_session=sessions[a],progress=lambda msg:events.put(('message',msg)),cancelled=cancel.is_set,documents=not assets_only),review=review,accounts=accounts,include_saved_pdfs=not assets_only)
            else:result=flow.run(month,review=review,accounts=accounts)
            events.put(('done',result))
        except Cancelled:events.put(('cancelled',None))
        except Exception as e:
            from ledger import LedgerError
            from login_credentials import CredentialError
            events.put(('error',str(e) if isinstance(e,(LedgerError,CredentialError)) else '接続または初回設定を確認してください。台帳への一括登録は完了していません。'))
    def finish():
        app.busy=False
        for b in app.buttons:b.configure(state='normal')
        app.refresh();dialog.destroy()
    def poll():
        while True:
            try:kind,data=events.get_nowait()
            except queue.Empty:break
            if kind=='progress':
                remember(PHASES.get(data['phase'],data['phase'])+' '+str(data.get('account','')))
                status.set(PHASES.get(data['phase'],data['phase'])+('　口座'+data['account'] if data.get('account') else ''))
                if data['phase']=='COMMITTING':cancel_button.configure(state='disabled');dialog.protocol('WM_DELETE_WINDOW',lambda:None)
            elif kind=='message':
                remember(data)
                details.insert('end',data+'\n');details.see('end')
            elif kind=='review':
                summary,proposal,prepared=data
                status.set('登録前の最終確認：以下を確認して「登録してExcel出力」を押してください。')
                details.insert('end',f"追加売買 {summary['new_trades']}件／配当・分割等 {summary['new_cash_or_splits']}件／残高報告 {summary['balance_reports']}件\n登録済PDF {summary['known_pdf_files']}件／対象外 {summary['excluded_pdf_files']}件\n\n")
                details.insert('end',f"資産取得を確認できた口座：{summary['assets_observed']}口座（指定日・項目の不足は完了時に表示）\n")
                for p in proposal:details.insert('end',f"{p['account']}　{p['name']}　{p['state']}\n")
                def accept():
                    choice[0]=True;decision.set();register.configure(state='disabled');preview.configure(state='disabled')
                register=ttk.Button(actions,text='登録してExcel出力',command=accept);register.pack(side='left',padx=8)
                preview=ttk.Button(actions,text='登録前Excelを確認',command=lambda:os.startfile(prepared));preview.pack(side='left',padx=8)
            elif kind=='done':
                try:write_diagnostics()
                except OSError:pass
                finish();warnings=data['warnings']
                note='\n保存済み資料から更新しました（SBIへの接続なし）。' if 'SAVED_FILES_ONLY' in warnings else ''
                assets=[x[-1] for x in warnings if x.startswith('ASSETS_NOT_OBSERVED_')]
                if assets:note+='\n資産額未取得：'+','.join(assets)+'。必要な基準日の実額を手入力してください。'
                labels={'domestic':'国内株式等','foreign':'外国株式（円評価）','cash':'円の現金合計','foreign_cash':'外貨現金（円評価）','growth_remaining':'NISA成長枠','tsumitate_remaining':'NISA積立枠','usd_cash':'米ドル現金（元通貨）'}
                for warning in warnings:
                    if warning.startswith('ASSET_FIELDS_MISSING:'):
                        _,account,keys=warning.split(':',2)
                        note+='\n口座'+account+'の未取得項目：'+'、'.join(labels.get(k,k) for k in keys.split(','))
                missing=[x.split(':',1)[1] for x in warnings if x.startswith('ASSET_DATE_INCOMPLETE:')]
                if missing:note+='\n指定日の資産に未取得項目あり：'+', '.join(missing)
                messagebox.showinfo('更新完了',str(data['output'])+note);return
            elif kind in ('error','cancelled'):
                finish()
                if kind=='error':
                    remember(data)
                    try:log=write_diagnostics();data+='\n\n診断ファイル：'+str(log)
                    except OSError:pass
                    messagebox.showerror('更新を停止',data)
                else:messagebox.showinfo('中止','台帳への一括登録を中止しました。収集済みPDFは次回に利用できます。')
                return
        app.root.after(100,poll)
    threading.Thread(target=worker,daemon=True).start();poll()
