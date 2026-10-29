"""Focused desktop workflow: collect four accounts, review, and export."""
from datetime import datetime,timedelta,timezone
from decimal import Decimal,InvalidOperation
from pathlib import Path
import hashlib
import os
import subprocess
import sys
import tkinter as tk
from tkinter import ttk,filedialog,messagebox,simpledialog
import webbrowser

from ledger import Store,LedgerError
from standalone_excel import export_trades
from activity import Activities
from login_credentials import (CredentialError,delete_credentials,has_credentials,
                               save_credentials)
from version import APP_VERSION

BASE=Path(__file__).resolve().parent
JST=timezone(timedelta(hours=9))
PRIMARY_ACTION='4口座のPDF・円・米ドル・NISAをまとめて取得'
MANUAL_CASH_KINDS=('税還','利息')


def start_schedule_bootstrap(base=BASE,popen=subprocess.Popen,platform_name=None):
    """Legacy compatibility entry; the desktop no longer calls this helper."""
    if (platform_name or os.name)!='nt':return None
    try:
        return popen([sys.executable,str(Path(base)/'windows_tasks.py'),'--action','bootstrap'],
                     cwd=str(base),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except OSError:return None


def retire_background_tasks(base=BASE):
    from schedule_retirement import retire_legacy_schedules
    return retire_legacy_schedules(base)


def add_manual_cash(activities,account,event_date,kind,amount):
    """The simplified form cannot accidentally create a dividend entry."""
    if kind not in MANUAL_CASH_KINDS:
        raise LedgerError('区分は税還または利息を選んでください。配当はPDFから取り込みます。')
    stored_kind='税還付' if kind=='税還' else '利息'
    from ledger import parse_date
    normalized_day=parse_date(event_date)
    try:
        normalized_amount=Decimal(str(amount).replace(',','').strip())
        if not normalized_amount.is_finite() or normalized_amount<=0:raise ValueError()
    except (InvalidOperation,ValueError):raise LedgerError('金額は0より大きい円額を入力してください。') from None
    # The four-field form cannot distinguish a second entry from a repeat of
    # an already imported PDF credit. Keep its original evidence instead.
    for event in activities.events():
        if (event.get('account')==account and event.get('date')==normalized_day
                and event.get('type')==stored_kind and event.get('currency','JPY')=='JPY'):
            value=event.get('amount',event.get('net'))
            try:
                if Decimal(str(value).replace(',',''))==normalized_amount:return False
            except (InvalidOperation,ValueError):pass
    return activities.add_cash(account,stored_kind,event_date,amount,stored_kind)


def archive_father_pdfs(base,store,paths):
    """Archive valid originals only to E; readable foreign owners never enter E."""
    from epost_collector import MAX_PDF_BYTES,save_pdf
    from pdf_batch import classify,ArchivedUnparsedDocument,KnownDocumentExclusion
    actions=Activities(store)
    profile=next((row for row in store.accounts() if row['id']=='E'),None)
    if profile is None:raise LedgerError('父の口座Eを確認できません。')
    result={'saved':0,'known':0,'errors':[]}
    for value in paths:
        source=Path(value)
        try:
            if source.stat().st_size>MAX_PDF_BYTES:raise LedgerError('PDFの保存上限を超えています。')
            raw=source.read_bytes()
            try:
                _,document=classify(source)
                if document.digest!=hashlib.sha256(raw).hexdigest():raise LedgerError('確認中にPDFが変更されました。')
                owner=document.owner
            except (ArchivedUnparsedDocument,KnownDocumentExclusion) as retained:
                owner=getattr(retained,'owner',None)
            if owner:
                actions._owner('E',owner)
                from activity import normalized_name
                expected=profile.get('owner_name') or profile['label']
                if normalized_name(expected)!=normalized_name(owner):raise LedgerError('父のPDF名義と一致しません。設定から父の氏名を確認してください。')
            if hashlib.sha256(source.read_bytes()).digest()!=hashlib.sha256(raw).digest():raise LedgerError('確認中にPDFが変更されました。')
            _,created=save_pdf(Path(base)/'data'/'電子ポストPDF','E','手動選択',source.stem,raw)
            result['saved' if created else 'known']+=1
        except (LedgerError,OSError,ValueError) as error:
            reason=str(error) if isinstance(error,LedgerError) else 'PDFを読み取り・保存できません。'
            result['errors'].append({'name':source.name,'reason':reason})
    return result


def display_number(value):
    try:
        number=Decimal(str(value))
        if not number.is_finite():return '—'
        rendered=format(number,',f')
        return rendered.rstrip('0').rstrip('.') if '.' in rendered else rendered
    except (InvalidOperation,ValueError,TypeError):return '' if value is None else str(value)


class App:
    def __init__(self,root):
        from data_backup import backup_before_upgrade
        self.upgrade_backup=backup_before_upgrade(BASE,APP_VERSION)
        self.root=root;self.store=Store(BASE/'data');self.activities=Activities(self.store);self.busy=False
        from family_profile import configure
        configure(self.store)
        self.accounts=self.store.accounts();self.buttons=[]
        root.title(f'家族の売買記録 v{APP_VERSION}');root.geometry('1180x760');root.minsize(1020,650)
        root.configure(background='#f3f6fa')
        style=ttk.Style();style.theme_use('clam')
        style.configure('.',font=('Yu Gothic UI',10),foreground='#17324d')
        style.configure('TFrame',background='#f3f6fa')
        style.configure('TLabel',background='#f3f6fa')
        style.configure('Title.TLabel',font=('Yu Gothic UI',21,'bold'))
        style.configure('Muted.TLabel',foreground='#52677d')
        style.configure('Card.TFrame',background='#ffffff')
        style.configure('Card.TLabel',background='#ffffff')
        style.configure('Primary.TButton',font=('Yu Gothic UI',13,'bold'),padding=(18,14),foreground='#ffffff',background='#174a7b')
        style.map('Primary.TButton',background=[('active','#246296'),('disabled','#b4c2d0')],foreground=[('disabled','#edf2f7')])
        style.configure('Treeview',rowheight=30,font=('Yu Gothic UI',10))
        style.configure('Treeview.Heading',font=('Yu Gothic UI',10,'bold'))
        outer=ttk.Frame(root,padding=22);outer.pack(fill='both',expand=True)
        heading=ttk.Frame(outer);heading.pack(fill='x')
        ttk.Label(heading,text='家族の売買記録',style='Title.TLabel').pack(side='left')
        self.settings=ttk.Menubutton(heading,text='設定・補助')
        self.settings.pack(side='right');self.buttons.append(self.settings)
        self._settings_menu()
        ttk.Label(outer,text='4口座をまとめて取得し、内容を確認してExcelへ。',style='Muted.TLabel').pack(anchor='w',pady=(4,16))
        card=ttk.Frame(outer,style='Card.TFrame',padding=16);card.pack(fill='x')
        button=ttk.Button(card,text=PRIMARY_ACTION,style='Primary.TButton',command=self.auto_collect)
        button.pack(fill='x');self.buttons.append(button)
        self.account_status=tk.StringVar()
        ttk.Label(card,textvariable=self.account_status,style='Card.TLabel',wraplength=1050).pack(anchor='w',pady=(10,2))
        ttk.Label(card,text='現金・NISA枠は取得時点の値を記録します。PDFは確認してから台帳へ反映します。',style='Card.TLabel',wraplength=1050).pack(anchor='w')
        secondary=ttk.Frame(outer);secondary.pack(fill='x',pady=(12,14))
        for label,command in [('父のPDF取り込み',self.import_father_pdfs),('税還・利息を手動で追加',self.add_cash_entry)]:
            button=ttk.Button(secondary,text=label,command=command,padding=(12,7))
            button.pack(side='left',padx=(0,8));self.buttons.append(button)
        ttk.Label(secondary,text='配当はPDFから自動で取り込みます。',style='Muted.TLabel').pack(side='left',padx=8)
        exports=ttk.Frame(outer);exports.pack(fill='x',pady=(0,12))
        ttk.Label(exports,text='出力対象月').pack(side='left')
        self.month=ttk.Entry(exports,width=10);self.month.pack(side='left',padx=8)
        ttk.Label(exports,text='YYYY-MM／空欄はすべて',style='Muted.TLabel').pack(side='left')
        button=ttk.Button(exports,text='Excelを出力',command=self.export_excel,padding=(12,6))
        button.pack(side='right');self.buttons.append(button)
        self.status=tk.StringVar();ttk.Label(outer,textvariable=self.status,style='Muted.TLabel').pack(anchor='w',pady=(0,8))
        self.migration_notice=tk.StringVar()
        ttk.Label(outer,textvariable=self.migration_notice,foreground='#8b5b13',wraplength=1050).pack(anchor='w')
        columns=('date','account','name','code','custody','side','qty','price','amount')
        frame=ttk.Frame(outer);frame.pack(fill='both',expand=True)
        self.tree=ttk.Treeview(frame,columns=columns,show='headings')
        for key,label,width in zip(columns,['約定日','口座','銘柄','コード','預り','売買','数量','単価','受渡金額'],[105,85,260,75,80,50,80,100,125]):
            self.tree.heading(key,text=label)
            self.tree.column(key,width=width,anchor='e' if key in ('qty','price','amount') else 'w')
        y=ttk.Scrollbar(frame,orient='vertical',command=self.tree.yview)
        x=ttk.Scrollbar(frame,orient='horizontal',command=self.tree.xview)
        self.tree.configure(yscrollcommand=y.set,xscrollcommand=x.set)
        self.tree.grid(row=0,column=0,sticky='nsew');y.grid(row=0,column=1,sticky='ns');x.grid(row=1,column=0,sticky='ew')
        frame.rowconfigure(0,weight=1);frame.columnconfigure(0,weight=1)
        ttk.Label(outer,text='同じ日の口座・銘柄・預り・売買が同じ取引は、1行にまとめて表示・出力します。',style='Muted.TLabel').pack(anchor='w',pady=(12,3))
        ttk.Label(outer,text='取引原本と口座設定は保持します。データはこのアプリの data フォルダに保存します。',style='Muted.TLabel').pack(anchor='w')
        root.protocol('WM_DELETE_WINDOW',self.close)
        self.refresh_accounts();self.refresh()
        from initial_setup import ensure_personal
        root.after(150,lambda:ensure_personal(BASE,root))
        root.after(800,lambda:retire_background_tasks(BASE))
        root.after(1500,self.check_retirement_status)

    def check_retirement_status(self):
        try:
            from schedule_retirement import read_retirement
            state=read_retirement(BASE)
            if not state:return
            failed=state.get('native_status')=='failed' or state.get('error_code') is not None or state.get('preferences_status')=='unreadable'
            if failed:
                self.migration_notice.set('以前の自動取得予約の停止を確認できていません。旧フォルダのWindowsタスクを確認してください。取得データは保持しています。')
            elif state.get('native_status') in ('pending','started'):
                self.migration_notice.set('以前の自動取得予約を停止しています。')
                self.root.after(1000,self.check_retirement_status)
            else:self.migration_notice.set('')
        except (OSError,ValueError):
            self.migration_notice.set('以前の自動取得予約の停止状況を確認できません。Windowsタスクを確認してください。')

    def _settings_menu(self):
        menu=tk.Menu(self.settings,tearoff=False)
        for label,command in [('口座名を変更',self.rename),('PDF名義を確認・変更',self.set_pdf_owner),('SBIログイン情報',self.set_login_credentials),('Gmail連携を設定',self.connect_gmail),('Gmailだけ診断',self.check_gmail)]:
            menu.add_command(label=label,command=command)
        menu.add_separator()
        for label,command in [('銘柄名・コードを設定',self.set_security_names),('配当予定・終値を確認',self.edit_market),('保存済みPDFから再作成',self.update_saved),('出力フォルダを開く',self.open_outputs),('診断ZIPを作成',self.export_diagnostics),('説明書を開く',self.open_guide)]:
            menu.add_command(label=label,command=command)
        self.settings.configure(menu=menu)

    def refresh_accounts(self):
        self.accounts=self.store.accounts()
        labels='・'.join(account['label'] for account in self.accounts if account['id'] in 'ABCD')
        if hasattr(self,'account_status'):self.account_status.set('取得する4口座：'+labels+'　／　父はPDFから取り込み')

    def choose_account(self,title,allowed='ABCDE'):
        """An account choice belongs to a settings action, never acquisition."""
        if self.busy:return None
        choices=[account for account in self.store.accounts() if account['id'] in allowed]
        if not choices:raise LedgerError('設定する口座を確認できません。')
        dialog=tk.Toplevel(self.root);dialog.title(title);dialog.transient(self.root);dialog.resizable(False,False)
        body=ttk.Frame(dialog,padding=18);body.pack(fill='both',expand=True)
        ttk.Label(body,text='設定する口座を選んでください。').pack(anchor='w',pady=(0,10))
        selection=ttk.Combobox(body,state='readonly',width=34,values=[account['id']+'：'+account['label'] for account in choices])
        selection.pack(fill='x');selection.current(0)
        result=[None]
        def submit():
            index=selection.current()
            if 0<=index<len(choices):result[0]=choices[index];dialog.destroy()
        actions=ttk.Frame(body);actions.pack(pady=(14,0))
        ttk.Button(actions,text='設定する',command=submit).pack(side='left',padx=5)
        ttk.Button(actions,text='キャンセル',command=dialog.destroy).pack(side='left',padx=5)
        dialog.grab_set();self.root.wait_window(dialog)
        return result[0]

    def selected(self):
        """Compatibility for external helpers; there is no main account selector."""
        account=self.choose_account('口座の設定')
        if account is None:raise LedgerError('口座の設定をキャンセルしました。')
        return account

    def refresh(self):
        from trade_report import group_trades
        for item in self.tree.get_children():self.tree.delete(item)
        original=self.store.records();records=group_trades(original)
        short={'買':'B','売':'S','特定':'特','NISA(成)':'N-成','NISA(積)':'N-積'}
        for record in records[-500:][::-1]:
            self.tree.insert('','end',values=(record['trade_date'],record['account_label'],record['name'],record.get('code',''),
                short.get(record.get('custody',''),record.get('custody','')),short.get(record['side'],record['side']),
                display_number(record['quantity']),display_number(record['price']),display_number(record['settlement_amount'])))
        self.status.set(f'保存済み {len(original):,}明細 → 同日合算 {len(records):,}行　／　最新500行を表示')

    def rename(self):
        account=self.choose_account('口座名を変更')
        if account is None:return
        name=simpledialog.askstring('口座名',f"口座{account['id']}の表示名",initialvalue=account['label'],parent=self.root)
        if name is not None:
            try:self.store.rename(account['id'],name);self.refresh_accounts();self.refresh()
            except LedgerError as error:messagebox.showerror('入力の確認',str(error),parent=self.root)

    def set_pdf_owner(self):
        account=self.choose_account('PDF名義を確認・変更')
        if account is None:return
        name=simpledialog.askstring('PDF名義',f"口座{account['id']}（{account['label']}）のPDFに記載された氏名",initialvalue=account.get('owner_name',''),parent=self.root)
        if name is not None:
            try:self.store.set_owner(account['id'],name);self.refresh_accounts();messagebox.showinfo('名義を設定しました','別の口座のPDFを誤って登録しないために使用します。',parent=self.root)
            except LedgerError as error:messagebox.showerror('入力の確認',str(error),parent=self.root)

    def set_login_credentials(self):
        account=self.choose_account('SBIログイン情報',allowed='ABCD')
        if account is None:return
        try:
            if has_credentials(BASE/'data',account['id']):
                choice=messagebox.askyesnocancel('ログイン情報は設定済みです',f'口座{account["id"]}：{account["label"]}\n\nはい：設定し直す\nいいえ：保存情報を削除\nキャンセル：変更しない',parent=self.root)
                if choice is None:return
                if choice is False:
                    delete_credentials(BASE/'data',account['id']);messagebox.showinfo('削除しました','保存済みログイン情報を削除しました。',parent=self.root);return
            username=simpledialog.askstring('SBIユーザーネーム',f'口座{account["id"]}：{account["label"]}のユーザーネーム',parent=self.root)
            if username is None:return
            password=simpledialog.askstring('SBIログインパスワード','この口座のログインパスワード',show='●',parent=self.root)
            if password is None:return
            confirmation=simpledialog.askstring('パスワードの確認','同じログインパスワードをもう一度入力',show='●',parent=self.root)
            if confirmation is None:return
            if password!=confirmation:raise CredentialError('2回のパスワードが一致しません。保存していません。')
            save_credentials(BASE/'data',account['id'],username,password)
            messagebox.showinfo('保存しました','このPCの同じWindowsユーザーだけが復号できます。',parent=self.root)
        except (CredentialError,OSError) as error:messagebox.showerror('保存できません',str(error),parent=self.root)

    def add_cash_entry(self):
        if self.busy:return
        choices=self.store.accounts()
        dialog=tk.Toplevel(self.root);dialog.title('税還・利息を手動で追加');dialog.transient(self.root);dialog.resizable(False,False)
        body=ttk.Frame(dialog,padding=18);body.pack(fill='both',expand=True)
        ttk.Label(body,text='PDFにない税還・利息を追加します。配当はPDFから取り込みます。',wraplength=430).grid(row=0,column=0,columnspan=2,sticky='w',pady=(0,12))
        values={'kind':tk.StringVar(value='税還'),'date':tk.StringVar(value=datetime.now(JST).date().isoformat()),'amount':tk.StringVar()}
        ttk.Label(body,text='口座').grid(row=1,column=0,sticky='w',padx=(0,12),pady=6)
        account=ttk.Combobox(body,state='readonly',values=[row['id']+'：'+row['label'] for row in choices],width=29)
        account.grid(row=1,column=1,sticky='ew',pady=6);account.current(0)
        for row,(label,key) in enumerate([('日付（YYYY-MM-DD）','date'),('区分','kind'),('金額（円）','amount')],2):
            ttk.Label(body,text=label).grid(row=row,column=0,sticky='w',padx=(0,12),pady=6)
            widget=(ttk.Combobox(body,textvariable=values[key],state='readonly',values=MANUAL_CASH_KINDS,width=29)
                    if key=='kind' else ttk.Entry(body,textvariable=values[key],width=31))
            widget.grid(row=row,column=1,sticky='ew',pady=6)
        def submit():
            try:
                index=account.current()
                if not 0<=index<len(choices):raise LedgerError('登録する口座を選んでください。')
                created=add_manual_cash(self.activities,choices[index]['id'],values['date'].get(),values['kind'].get(),values['amount'].get())
                if not created:messagebox.showinfo('登録済み','同じ口座・日付・区分・金額の記録は登録済みです。',parent=dialog)
                else:messagebox.showinfo('追加しました','配当シートに反映されます。Excelを出力してください。',parent=dialog)
                self.refresh();dialog.destroy()
            except LedgerError as error:messagebox.showerror('入力を確認してください',str(error),parent=dialog)
        buttons=ttk.Frame(body);buttons.grid(row=5,column=0,columnspan=2,pady=(14,0))
        ttk.Button(buttons,text='追加する',command=submit).pack(side='left',padx=6)
        ttk.Button(buttons,text='キャンセル',command=dialog.destroy).pack(side='left',padx=6)
        dialog.grab_set();self.root.wait_window(dialog)

    def import_father_pdfs(self):
        if self.busy:return
        try:
            initial=BASE/'data'/'電子ポストPDF'/'E';initial.mkdir(parents=True,exist_ok=True)
            paths=filedialog.askopenfilenames(title='父のPDFを取り込み',parent=self.root,initialdir=initial,filetypes=[('PDF','*.pdf')])
            if not paths:return
            result=archive_father_pdfs(BASE,self.store,paths)
            if result['errors']:
                message='\n'.join(item['name']+'：'+item['reason'] for item in result['errors'][:5])
                messagebox.showwarning('保存できないPDFがあります',message+'\n元ファイルは変更していません。',parent=self.root)
            if result['saved'] or result['known']:self.update_saved()
        except (LedgerError,OSError) as error:messagebox.showerror('父のPDF取り込み',str(error),parent=self.root)

    def import_many_pdfs(self):
        self.import_father_pdfs()

    def auto_collect(self):
        from update_dialog import launch
        launch(self,BASE,online=True,current_only=True)

    def update_saved(self):
        from update_dialog import launch
        launch(self,BASE,online=False,current_only=True)

    def export_excel(self):
        if self.busy:return
        try:
            target=export_trades(self.store,BASE/'output',self.month.get().strip(),self.activities)
            messagebox.showinfo('Excelを出力しました',f'{target}\n\n「取引」「配当」に出力しました。',parent=self.root)
        except (LedgerError,OSError,ValueError) as error:messagebox.showerror('出力できません',str(error),parent=self.root)

    def set_security_names(self):
        if self.busy:return
        from security_names import edit
        edit(self.root,self.store)

    def edit_market(self):
        if not self.busy:subprocess.Popen([sys.executable,str(BASE/'market_editor.py')],cwd=BASE)

    def check_gmail(self):
        if self.busy:return
        try:process=subprocess.Popen([sys.executable,str(BASE/'auth_check.py')],cwd=str(BASE),creationflags=getattr(subprocess,'CREATE_NEW_CONSOLE',0))
        except OSError:messagebox.showerror('Gmail診断','check_gmail_windows.batから実行してください。',parent=self.root);return
        self.busy=True
        for button in self.buttons:button.configure(state='disabled')
        def poll():
            if process.poll() is None:self.root.after(500,poll);return
            self.busy=False
            for button in self.buttons:button.configure(state='normal')
        self.root.after(500,poll)

    def connect_gmail(self):
        account=self.choose_account('Gmail連携を設定',allowed='ABCD')
        if account is None:return
        try:
            path=filedialog.askopenfilename(title='Google CloudのデスクトップOAuthクライアントJSON',parent=self.root,filetypes=[('JSON','*.json')])
            if not path:return
            recipient=simpledialog.askstring('受信先','このSBI口座に登録している認証メールの宛先',parent=self.root)
            if not recipient:return
            import re
            if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',recipient):raise LedgerError('メールアドレスを確認してください。')
            from gmail_auth import connect
            connect(BASE/'data',account['id'],path,recipient)
            messagebox.showinfo('連携しました','Gmailの認可情報を同じWindowsユーザー向けに保存しました。',parent=self.root)
        except Exception:messagebox.showerror('連携できません','Gmail設定・デスクトップOAuth設定・テストユーザーを確認してください。認証の詳細はログに出しません。',parent=self.root)

    def open_outputs(self):
        folder=BASE/'output';folder.mkdir(exist_ok=True)
        if os.name=='nt':os.startfile(folder)
        else:webbrowser.open(folder.as_uri())

    def export_diagnostics(self):
        if self.busy:return
        destination=filedialog.asksaveasfilename(title='診断ZIPを保存',parent=self.root,defaultextension='.zip',initialfile='family_trades_diagnostics.zip',filetypes=[('ZIP','*.zip')])
        if not destination:return
        try:
            from diagnostic_bundle import create_bundle
            result=create_bundle(BASE,Path(destination))
            messagebox.showinfo('診断ZIPを保存しました',str(result['path'])+'\n氏名・資産額が残る場合があるため、共有前に内容を確認してください。',parent=self.root)
        except (LedgerError,OSError,ValueError):messagebox.showerror('診断ZIPを作成できません','保存先・空き容量を確認してください。',parent=self.root)

    def open_guide(self):
        path=BASE/'はじめに.html'
        if os.name=='nt':os.startfile(path)
        else:webbrowser.open(path.as_uri())

    def close(self):
        if self.busy:messagebox.showinfo('取得中','現在の処理を中止するか、終了をお待ちください。',parent=self.root);return
        self.store.close();self.root.destroy()


if __name__=='__main__':
    root=tk.Tk()
    try:App(root);root.mainloop()
    except LedgerError as error:messagebox.showerror('起動を確認してください',str(error));root.destroy()
