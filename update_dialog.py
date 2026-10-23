"""Tk/main-thread UI and isolated background update worker."""
import threading,queue,os
from tkinter import ttk,messagebox
import tkinter as tk
from update_workflow import Workflow,Cancelled
from version import APP_VERSION

PHASES={'ACCOUNT_FAILED':'この口座の収集失敗・次の口座へ','AUTHORIZING':'Gmail連携を確認中','COLLECTING':'SBIから収集中','CHECKING_PDF':'PDFを検査中','PREPARING_EXCEL':'Excelを作成中','COMMITTING':'確認済みデータを登録中','DATA_COMMITTED':'登録完了・Excelを配置中','COMPLETED':'完了','COMPLETED_WITH_WARNINGS':'完了（未取得項目あり）','FAILED':'停止','CANCELLED':'中止','OUTPUT_PENDING':'登録完了・出力待ち'}

def warning_lines(warnings):
    lines=[]
    labels={'domestic':'国内株式等','foreign':'外国株式（円評価）','cash':'円の現金合計','foreign_cash':'外貨現金（円評価）','growth_remaining':'NISA成長枠','tsumitate_remaining':'NISA積立枠','usd_cash':'米ドル現金（元通貨）'}
    excluded=[x for x in warnings if x.startswith('PDF_NOT_PARSED:')]
    if excluded:lines.append(f'未解析PDF {len(excluded)}件：原本を保存したまま、この書類だけ集計から除外します。他の対応PDFと資産の反映は続けます。書類名と理由を下の一覧で確認してください。')
    for warning in dict.fromkeys(warnings):
        if warning=='SAVED_FILES_ONLY':lines.append('保存済みPDF・取得証跡から更新します。今回はSBIへ接続していません。')
        elif warning.startswith('NISA_TRANSFER_UNAPPLIED:'):
            _,account,_=warning.split(':',2)
            lines.append('口座'+account+'：NISA払出通知の口座区分移動・取得価額は未反映です。原本と既存記録を確認してください。')
        elif warning.startswith('ASSET_FIELDS_MISSING:'):
            _,account,keys=warning.split(':',2)
            lines.append('口座'+account+'の現在資産で未取得：'+'、'.join(labels.get(k,k) for k in keys.split(',')))
        elif warning.startswith('ASSET_DATE_INCOMPLETE:'):
            _,account,day=warning.split(':',2)
            if f'ASSET_DATE_OBSERVED:{account}:{day}' in warnings:continue
            lines.append('口座'+account+'・'+day+'：基準日を直接確認できない資産/NISAがあります。別日の現在値や推計で補完しません。')
        elif warning.startswith('ASSET_DATE_OBSERVED:'):
            _,account,day=warning.split(':',2)
            lines.append('口座'+account+'・'+day+'：当日実測の参考記録です。月末終値・月初開始時点ではなく、空欄は未取得です。Excelの確認状態に取得時刻を記載します。')
        elif warning.startswith('ASSET_DATE_LEGACY_ESTIMATE:'):
            _,account,day=warning.split(':',2)
            lines.append('口座'+account+'・'+day+'：旧版の前月比推定値を保持しています。基準時刻・暦日の残高としては未確認です。')
        elif warning.startswith(('DAILY_OBSERVATION:','DAILY_OBSERVATION_CONFLICT:')):
            parts=warning.split(':')
            if warning.endswith(':NO_SAME_DAY_OBSERVATION') and f'ASSET_DATE_INCOMPLETE:{parts[1]}:{parts[2]}' in warnings:continue
            line='口座'+parts[1]+'・'+parts[2]+'：当日の履歴に不足・時刻不一致・競合があり、確認できる項目だけを採用します。'
            if line not in lines:lines.append(line)
        elif warning.startswith('ASSETS_NOT_OBSERVED_'):lines.append('口座'+warning[-1]+'：今回、現在資産の全項目を確認できていません。')
        elif warning.startswith('MANUAL_ASSET_PRESERVED:'):lines.append('手動で登録済みの資産額を保持しました。取得証跡の一覧で確認してください。')
        elif not warning.startswith('PDF_NOT_PARSED:'):lines.append(str(warning))
    return lines

def launch(app,base,online=False,accounts=None,assets_only=False,acquire_only=False,force_pdf_refresh=False):
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
    dialog=tk.Toplevel(app.root);dialog.title('選択口座のPDF取得（未反映）' if acquire_only else '資産の自動取得' if assets_only else '全口座をまとめて更新');dialog.geometry('880x580');dialog.transient(app.root)
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
        file=folder/('auto_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.txt')
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
            from update_operations import collect_and_apply,acquire_selected_pdf,apply_saved
            progress=lambda **x:events.put(('progress',x))
            message=lambda msg:events.put(('message',msg))
            if online:
                from playwright.sync_api import sync_playwright
                from auto_login import run_one
                if force_pdf_refresh:
                    from functools import partial
                    run_one=partial(run_one,force_pdf_refresh=True)
                from gmail_auth import ensure_service
                with sync_playwright() as pw:
                    if acquire_only:
                        if not accounts or len(accounts)!=1:raise CredentialError('PDF取得する口座を1つ選んでください。')
                        result=acquire_selected_pdf(base,accounts[0],pw,ensure_service,run_one,message=message,cancelled=cancel.is_set)
                        events.put(('acquired',result));return
                    if assets_only:
                        flow=Workflow(base,progress=progress,cancelled=cancel.is_set)
                        def asset_collect(account,stage):
                            if cancel.is_set():raise Cancelled()
                            session=ensure_service(base/'data',account,message)
                            return run_one(pw,account,stage,gmail_session=session,progress=message,cancelled=cancel.is_set,documents=False,target_dates=flow.target_dates)
                        result=flow.run(month,collect=asset_collect,review=review,accounts=accounts,include_saved_pdfs=False)
                    else:result=collect_and_apply(base,month,pw,ensure_service,run_one,review,progress=progress,message=message,cancelled=cancel.is_set)
            else:result=apply_saved(base,month,review,progress=progress,cancelled=cancel.is_set)
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
                details.insert('end',f"追加売買 {summary['new_trades']}件／配当・分割等 {summary['new_cash_or_splits']}件／残高報告 {summary['balance_reports']}件\n登録済PDF {summary['known_pdf_files']}件／保管のみ・集計対象外 {summary['excluded_pdf_files']}件\n\n")
                details.insert('end',f"現在資産の全項目を確認：{summary['assets_observed']}口座／保存済み取得証跡：{summary.get('recovered_asset_accounts',0)}口座\n")
                if summary.get('daily_asset_records'):details.insert('end',f"月末・月初の当日実測（参考記録）：{summary['daily_asset_records']}件。終値・開始時点との区別を確認してください。\n")
                warnings=summary.get('warnings',[])
                if warnings:
                    lines=warning_lines(warnings)
                    for line in lines:remember(line)
                    details.insert('end','\n【未解析・未取得の確認】\n'+'\n'.join(lines)+'\n\n')
                for p in proposal:
                    line=f"{p['account']}　{p['name']}　{p['state']}"
                    remember(line);details.insert('end',line+'\n')
                def accept():
                    choice[0]=True;decision.set();register.configure(state='disabled');preview.configure(state='disabled')
                register=ttk.Button(actions,text='登録してExcel出力',command=accept);register.pack(side='left',padx=8)
                preview=ttk.Button(actions,text='登録前Excelを確認',command=lambda:os.startfile(prepared));preview.pack(side='left',padx=8)
            elif kind=='acquired':
                try:write_diagnostics()
                except OSError:pass
                finish()
                messagebox.showinfo('PDF取得完了・台帳には未反映',f"口座{data['account']}：新規保存 {data['saved']}件／保存済み {data['duplicate']}件。\n「取得済みPDFを全口座へ一括反映」でまとめて確認してください。")
                return
            elif kind=='done':
                try:write_diagnostics()
                except OSError:pass
                finish();warnings=data['warnings']
                lines=warning_lines(warnings)
                note='\n'+'\n'.join(lines[:10]) if lines else ''
                if len(lines)>10:note+='\n残りの確認事項は今回の診断ファイルに記録しています。'
                (messagebox.showwarning if warnings else messagebox.showinfo)('反映完了（確認事項あり）' if warnings else '資産の反映完了' if assets_only else '全口座の反映完了',str(data['output'])+note);return
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
