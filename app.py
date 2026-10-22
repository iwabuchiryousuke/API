"""Desktop UI. Start with start_windows.bat or python app.py."""
from pathlib import Path
import os
import subprocess
import sys
import tkinter as tk
from tkinter import ttk,filedialog,messagebox,simpledialog
import webbrowser
from ledger import Store,LedgerError
from standalone_excel import export_trades
from activity import Activities
from pdf_batch import preflight as preflight_pdfs,import_all as import_all_pdfs
from login_credentials import (CredentialError,delete_credentials,has_credentials,
                               save_credentials)

BASE=Path(__file__).resolve().parent
from version import APP_VERSION

class App:
    def __init__(self,root):
        from data_backup import backup_before_upgrade
        self.upgrade_backup=backup_before_upgrade(BASE,APP_VERSION)
        self.root=root;self.store=Store(BASE/'data');self.activities=Activities(self.store);self.busy=False
        root.title(f'家族の売買記録 v{APP_VERSION} — SBI・SMBC PDF取り込み');root.geometry('1180x740');root.minsize(1060,660)
        style=ttk.Style();style.theme_use('clam');style.configure('.',font=('Yu Gothic UI',10));style.configure('Title.TLabel',font=('Yu Gothic UI',17,'bold'))
        outer=ttk.Frame(root,padding=22);outer.pack(fill='both',expand=True)
        ttk.Label(outer,text=f'家族の売買記録　v{APP_VERSION}',style='Title.TLabel').pack(anchor='w')
        ttk.Label(outer,text='A～EのPDF・配当・分割を記録 → 「取引」「配当」の2シートを出力').pack(anchor='w',pady=(5,12))
        from family_profile import configure
        configure(self.store)
        self.buttons=[]
        bar=ttk.Frame(outer);bar.pack(fill='x')
        ttk.Label(bar,text='取り込み先の口座').pack(side='left')
        self.account=ttk.Combobox(bar,state='readonly',width=29);self.account.pack(side='left',padx=8)
        for label,fn in [('口座名を設定',self.rename),('銘柄名・コードを設定',self.set_security_names),('PDFを取り込む（複数可）',self.import_many_pdfs)]:
            b=ttk.Button(bar,text=label,command=fn);b.pack(side='left',padx=4);self.buttons.append(b)
        ttk.Label(outer,text='PDFは選択口座の data／電子ポストPDF／A～E を最初に開きます。名義は別口座の誤登録を止める確認項目です。').pack(anchor='w',pady=(10,0))
        epost=ttk.Frame(outer);epost.pack(fill='x',pady=(8,0))
        for label,fn in [('選択口座を自動取得',self.collect_epost),('保存したPDFのフォルダを開く',self.open_collected_pdfs),('PDF取得結果一覧',self.open_collection_audit),('配当・税還・利息を手動追加',self.add_cash_entry),('分割を登録',self.add_split)]:
            b=ttk.Button(epost,text=label,command=fn);b.pack(side='left',padx=4);self.buttons.append(b)
        extra=ttk.Frame(outer);extra.pack(fill='x',pady=(8,0))
        for label,fn in [('4口座収集→確認→登録→出力',self.auto_collect),('資産額を記録',self.record_assets),('Gmailだけ診断',self.check_gmail),('説明書を開く',self.open_guide)]:
            b=ttk.Button(extra,text=label,command=fn);b.pack(side='left',padx=4);self.buttons.append(b)
        settings=ttk.Menubutton(extra,text='口座・認証の設定')
        menu=tk.Menu(settings,tearoff=False)
        for label,fn in [('PDF名義を確認・変更',self.set_pdf_owner),('SBIログイン情報',self.set_login_credentials),('Gmail連携を設定',self.connect_gmail)]:
            menu.add_command(label=label,command=fn)
        settings.configure(menu=menu);settings.pack(side='left',padx=4);self.buttons.append(settings)
        unified=ttk.Frame(outer);unified.pack(fill='x',pady=(8,0))
        b=ttk.Button(unified,text='保存PDFをまとめて更新（A～E）',command=self.update_saved);b.pack(side='left',padx=4);self.buttons.append(b)
        b=ttk.Button(unified,text='選択口座の資産だけ自動取得',command=self.auto_assets);b.pack(side='left',padx=4);self.buttons.append(b)
        b=ttk.Button(unified,text='取得した資産を確認',command=self.show_assets);b.pack(side='left',padx=4);self.buttons.append(b)
        self.refresh_accounts()
        exportbar=ttk.Frame(outer);exportbar.pack(fill='x',pady=16)
        ttk.Label(exportbar,text='対象月末まで（空欄＝全期間）').pack(side='left')
        self.month=ttk.Entry(exportbar,width=10);self.month.pack(side='left',padx=8)
        ttk.Label(exportbar,text='例：2026-05').pack(side='left')
        for label,fn in [('売買記録Excelを出力',self.export_excel),('出力フォルダを開く',self.open_outputs),('配当予定・終値の確認',self.edit_market)]:
            b=ttk.Button(exportbar,text=label,command=fn);b.pack(side='left',padx=5);self.buttons.append(b)
        self.status=tk.StringVar();ttk.Label(outer,textvariable=self.status).pack(anchor='w',pady=(0,10))
        cols=('account','date','name','side','qty','price','amount')
        frame=ttk.Frame(outer);frame.pack(fill='both',expand=True)
        self.tree=ttk.Treeview(frame,columns=cols,show='headings')
        for c,label,w in zip(cols,['口座','約定日','銘柄','売買','数量','単価','受渡金額'],[140,100,230,50,75,100,120]):
            self.tree.heading(c,text=label);self.tree.column(c,width=w,anchor='e' if c in ('qty','price','amount') else 'w')
        scroll=ttk.Scrollbar(frame,orient='vertical',command=self.tree.yview);self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side='left',fill='both',expand=True);scroll.pack(side='right',fill='y')
        ttk.Label(outer,text='PDF専用。未対応の書類は理由を表示。既存台帳の過去取引は保持します。',foreground='#8b5b13').pack(anchor='w',pady=(14,4))
        ttk.Label(outer,text='データはこのフォルダ内の data に保存。フォルダごとバックアップしてください。').pack(anchor='w')
        root.protocol('WM_DELETE_WINDOW',self.close);self.refresh()
        from initial_setup import ensure_personal
        root.after(150,lambda:ensure_personal(BASE,root))

    def check_gmail(self):
        import subprocess,sys
        if self.busy:return
        try:process=subprocess.Popen([sys.executable,str(BASE/'auth_check.py')],cwd=str(BASE),creationflags=getattr(subprocess,'CREATE_NEW_CONSOLE',0))
        except OSError:
            messagebox.showerror('Gmail診断','診断を起動できません。check_gmail_windows.batから実行してください。');return
        self.busy=True
        for button in self.buttons:button.configure(state='disabled')
        def poll():
            if process.poll() is None:self.root.after(500,poll);return
            self.busy=False
            for button in self.buttons:button.configure(state='normal')
        self.root.after(500,poll)

    def refresh_accounts(self):
        old=self.account.current();self.accounts=self.store.accounts()
        self.account['values']=[f"{a['id']}：{a['label']}" for a in self.accounts]
        if old>=0:self.account.current(old)

    def selected(self):
        i=self.account.current()
        if i<0:raise LedgerError('最初に取り込み先の口座を選んでください。PDF名義も確認します。')
        return self.accounts[i]

    def refresh(self):
        for item in self.tree.get_children():self.tree.delete(item)
        records=self.store.records()
        for t in records[-500:][::-1]:self.tree.insert('','end',values=(t['account_label'],t['trade_date'],t['name'],t['side'],t['quantity'],t['price'],t['settlement_amount']))
        counts={a['id']:sum(t['account']==a['id'] for t in records) for a in self.accounts}
        self.status.set(f"保存済み {len(records)}件 ／ "+'  '.join(f'{k}：{v}件' for k,v in counts.items())+' ／ 最新500件を表示')

    def rename(self):
        try:a=self.selected()
        except LedgerError as e:messagebox.showinfo('口座を選択',str(e));return
        name=simpledialog.askstring('口座名',f"口座{a['id']}の名前（本人・家族名など）",initialvalue=a['label'])
        if name is not None:
            try:self.store.rename(a['id'],name);self.refresh_accounts();self.refresh()
            except LedgerError as e:messagebox.showerror('入力の確認',str(e))

    def set_pdf_owner(self):
        try:a=self.selected()
        except LedgerError as e:messagebox.showinfo('口座を選択',str(e));return
        name=simpledialog.askstring('取引報告書の名義',f"口座{a['id']}（{a['label']}）のPDFに記載された氏名を入力してください。",initialvalue=a.get('owner_name',''))
        if name is not None:
            try:self.store.set_owner(a['id'],name);self.refresh_accounts();messagebox.showinfo('名義を設定しました','PDFに記載された氏名を登録しました。別口座のPDFを選んだときに登録を止めるために使います。')
            except LedgerError as e:messagebox.showerror('入力の確認',str(e))

    def set_login_credentials(self):
        try:a=self.selected()
        except LedgerError as error:messagebox.showinfo('口座を選択',str(error));return
        try:
            if has_credentials(BASE/'data',a['id']):
                choice=messagebox.askyesnocancel('ログイン情報は設定済みです',
                    f'口座{a["id"]}：{a["label"]}は設定済みです。\n\n'
                    '「はい」＝設定し直す\n「いいえ」＝保存情報を削除\n「キャンセル」＝何もしない')
                if choice is None:return
                if choice is False:
                    delete_credentials(BASE/'data',a['id'])
                    messagebox.showinfo('削除しました','この口座の保存済みログイン情報を削除しました。')
                    return
            username=simpledialog.askstring('SBIユーザーネーム',
                f'口座{a["id"]}：{a["label"]}のユーザーネームを入力してください。\n'
                'Excelやログには出力しません。')
            if username is None:return
            password=simpledialog.askstring('SBIログインパスワード',
                'ログインパスワードを入力してください。\n同じWindowsユーザーだけが復号できる形式で保存します。',show='●')
            if password is None:return
            confirmation=simpledialog.askstring('パスワードの確認','同じログインパスワードをもう一度入力してください。',show='●')
            if confirmation is None:return
            if password!=confirmation:raise CredentialError('2回のパスワードが一致しません。保存していません。')
            save_credentials(BASE/'data',a['id'],username,password)
            messagebox.showinfo('安全に保存しました',
                'このPCの現在のWindowsユーザーだけが復号できます。\n'
                '自動取得時は、この口座のGmail連携と名義を確認してから利用します。')
        except (CredentialError,OSError) as error:messagebox.showerror('保存できません',str(error))

    def add_cash_entry(self):
        try:a=self.selected()
        except LedgerError as error:messagebox.showinfo('口座を選択',str(error));return
        dialog=tk.Toplevel(self.root);dialog.title('配当・税還付・利息を手動追加');dialog.transient(self.root);dialog.resizable(False,False)
        body=ttk.Frame(dialog,padding=18);body.pack(fill='both',expand=True)
        values={'kind':tk.StringVar(value='税還付'),'date':tk.StringVar(),'description':tk.StringVar(),'amount':tk.StringVar(),'code':tk.StringVar(),'custody':tk.StringVar(),'tax':tk.StringVar(value='0'),'quantity':tk.StringVar(),'per_share':tk.StringVar()}
        for row,(label,key) in enumerate([('区分','kind'),('日付（例：2024-02-16）','date'),('内容','description'),('受取額（円）','amount'),('コード（配当）','code'),('預り（配当）','custody'),('税額（配当）','tax'),('株数（任意）','quantity'),('一株配当（任意）','per_share')]):
            ttk.Label(body,text=label).grid(row=row,column=0,sticky='w',padx=(0,12),pady=6)
            if key=='kind':widget=ttk.Combobox(body,textvariable=values[key],state='readonly',values=('配当','税還付','利息'),width=28)
            else:widget=ttk.Entry(body,textvariable=values[key],width=31)
            widget.grid(row=row,column=1,sticky='ew',pady=6)
        ttk.Label(body,text=f'登録先：口座{a["id"]}・{a["label"]}。PDFで自動取得できない入金だけに使います。').grid(row=9,column=0,columnspan=2,sticky='w',pady=(8,10))
        saved={'ok':False}
        def submit():
            try:
                created=self.activities.add_cash(a['id'],values['kind'].get(),values['date'].get(),values['amount'].get(),values['description'].get(),**{k:values[k].get() for k in ('code','custody','tax','quantity','per_share')})
                if not created:messagebox.showinfo('登録済み','同じ日付・区分・内容・金額の記録は登録済みです。',parent=dialog)
                else:messagebox.showinfo('追加しました','次のExcel出力に反映されます。',parent=dialog)
                saved['ok']=True;dialog.destroy()
            except LedgerError as error:messagebox.showerror('入力を確認してください',str(error),parent=dialog)
        buttons=ttk.Frame(body);buttons.grid(row=10,column=0,columnspan=2,pady=(4,0))
        ttk.Button(buttons,text='追加する',command=submit).pack(side='left',padx=6)
        ttk.Button(buttons,text='やめる',command=dialog.destroy).pack(side='left',padx=6)
        dialog.grab_set();self.root.wait_window(dialog)

    def open_outputs(self):
        folder=BASE/'output'
        folder.mkdir(exist_ok=True)
        if os.name=='nt':os.startfile(folder)
        else:webbrowser.open(folder.as_uri())

    def collect_epost(self):
        try:
            a=self.selected()
            if a['id']=='E':raise LedgerError('父のPDFは保存フォルダへ手動で入れてください。')
            from update_dialog import launch
            launch(self,BASE,online=True,accounts=[a['id']])
        except LedgerError as e:messagebox.showerror('口座を確認',str(e))

    def set_security_names(self):
        from security_names import edit
        edit(self.root,self.store)

    def add_split(self):
        from split_entry import edit
        edit(self)

    def open_collected_pdfs(self):
        try:a=self.selected()
        except LedgerError as error:messagebox.showinfo('口座を選択',str(error));return
        folder=BASE/'data'/'電子ポストPDF'/a['id'];folder.mkdir(parents=True,exist_ok=True)
        if os.name=='nt':os.startfile(folder)
        else:webbrowser.open(folder.as_uri())

    def open_collection_audit(self):
        try:a=self.selected()
        except LedgerError as e:messagebox.showinfo('口座を選択',str(e));return
        folder=BASE/'data'/'電子ポストPDF'/a['id']
        files=sorted(folder.glob('取得結果_*.html'))
        if not files:messagebox.showinfo('取得結果','この版でPDF収集すると、書類ごとの結果が作成されます。');return
        webbrowser.open(files[-1].resolve().as_uri())

    def import_many_pdfs(self):
        try:
            a=self.selected()
            initial=BASE/'data'/'電子ポストPDF'/a['id'];initial.mkdir(parents=True,exist_ok=True)
            paths=filedialog.askopenfilenames(title=f'口座{a["id"]}・{a["label"]}のPDFを選択（複数可）',initialdir=initial,filetypes=[('PDF','*.pdf')])
            if not paths:return
            pending,skipped=preflight_pdfs(self.store,a['id'],paths,skip_unreadable=True)
            if not pending:
                details='\n'.join(f'{p.name}：{reason}' for p,reason in skipped)
                messagebox.showwarning('登録できるPDFがありません',
                    f'{len(skipped)}件は登録対象外・解析済み0円・未対応等でした。売買記録は変更していません。\n\n{details[:3000]}')
                return
            lines=[]
            for path,kind,doc,added,existing,already in pending:
                if kind=='trade':
                    description=f'{len(doc.trades)}取引（新規 {added}件／登録済みと照合 {existing}件）\n'+'\n'.join(
                        f'      {t["trade_date"]} {t["name"]} {t["side"]}{t["quantity"]}{t.get("quantity_unit","株")} {t["settlement_amount"]}円 [{t["market"]}]'
                        for t in doc.trades)
                elif kind=='notice':
                    e=doc.item;description=f'{e["date"]} {e["name"]} {e["type"]} '+(f'{e["after"]}株' if e['type']=='株式分割' else f'{e["net"]}円')
                else:
                    description=f'{doc.item["as_of"]} 残高報告書 {len(doc.item["holdings"])}銘柄'
                    if doc.item.get('warning'):description+=f'（{doc.item["warning"]}）'
                state='登録済み' if already else '資料を追加' if existing and not added else '新規と照合あり' if existing else '新規'
                owner=f'名義：{doc.owner}' if doc.owner else '名義：原本で確認'
                lines.append(f'{path.name}\n    {description}　{state}　{owner}')
            if skipped:
                lines.append('【登録しないPDF：対象外または未対応】\n'
                    +'\n'.join(f'{p.name}：{reason}' for p,reason in skipped))
            dialog=tk.Toplevel(self.root);dialog.title('PDFの登録内容を確認');dialog.geometry('850x500');dialog.transient(self.root)
            ttk.Label(dialog,text=f'登録先：{a["id"]}：{a["label"]} ／ 登録可能 {len(pending)}件・未対応 {len(skipped)}件',font=('Yu Gothic UI',12,'bold')).pack(anchor='w',padx=15,pady=10)
            ttk.Label(dialog,text='残高報告書は名義を自動照合できません。すべてのPDFの口座・内容を原本で確認してください。').pack(anchor='w',padx=15)
            box=tk.Text(dialog,wrap='word');box.pack(fill='both',expand=True,padx=15,pady=10)
            box.insert('1.0','\n\n'.join(lines));box.configure(state='disabled')
            confirmed={'yes':False}
            row=ttk.Frame(dialog);row.pack(pady=(0,12))
            def decide(ok):confirmed['yes']=ok;dialog.destroy()
            ttk.Button(row,text='登録する',command=lambda:decide(True)).pack(side='left',padx=8)
            ttk.Button(row,text='やめる',command=lambda:decide(False)).pack(side='left',padx=8)
            dialog.grab_set();self.root.wait_window(dialog)
            if confirmed['yes']:
                results=import_all_pdfs(self.store,a['id'],pending);self.refresh()
                messagebox.showinfo('PDFを登録しました',f'{len(results)}件のPDFを確認しました。新規資料・取引はExcel再出力で確認できます。')
        except (LedgerError,OSError) as e:messagebox.showerror('PDFを登録できません',str(e))

    def edit_market(self):
        subprocess.Popen([sys.executable,str(BASE/'market_editor.py')],cwd=BASE)

    def open_guide(self):
        os.startfile(BASE/'はじめに.html')

    def export_management(self):
        try:
            target=export_trades(self.store,BASE/'output',self.month.get().strip(),self.activities,management=True)
            messagebox.showinfo('管理見本',f'{target}\n灰色のコード・株価を入力してください。既存の管理ファイルは上書きしません。')
        except Exception as e:messagebox.showerror('出力できません',str(e))

    def auto_collect(self):
        from update_dialog import launch
        launch(self,BASE,online=True)

    def update_saved(self):
        from update_dialog import launch
        launch(self,BASE,online=False)

    def connect_gmail(self):
        try:
            a=self.selected()
            if a['id']=='E':raise LedgerError('Gmail自動収集はSBIのA～D口座向けです。')
            path=filedialog.askopenfilename(title='Google CloudのデスクトップOAuthクライアントJSON',filetypes=[('JSON','*.json')])
            if not path:return
            recipient=simpledialog.askstring('受信先','このSBI口座に登録している認証メールの宛先を入力してください。')
            if not recipient:return
            import re
            if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',recipient):raise LedgerError('メールアドレスを確認してください。')
            from gmail_auth import connect
            connect(BASE/'data',a['id'],path,recipient)
            messagebox.showinfo('連携しました','Googleの認可情報をWindows暗号化で保存しました。ダウンロードした元のJSONは配布フォルダ外で保管してください。')
        except Exception:
            messagebox.showerror('連携できません','setup_gmail_windows.bat の実行、Google CloudのデスクトップOAuth設定、テストユーザーを確認してください。認証の詳細はログに出しません。')

    def auto_assets(self):
        try:
            account=self.selected()['id']
            if account=='E':raise LedgerError('資産の自動取得はSBIのA～D口座向けです。')
        except LedgerError as error:messagebox.showinfo('口座を確認',str(error));return
        from update_dialog import launch
        launch(self,BASE,online=True,accounts=(account,),assets_only=True)

    def show_assets(self):
        from sbi_views import latest,_jst_now
        from asset_snapshots import combine_assets
        observations=latest(self.store,_jst_now().date().isoformat())
        dialog=tk.Toplevel(self.root);dialog.title('取得時点の資産');dialog.geometry('1120x360');dialog.transient(self.root)
        ttk.Label(dialog,text='取得時点の表示値です。未取得は空のまま保持し、過去の月末・月初へ代用しません。',padding=12).pack(anchor='w')
        body=ttk.Frame(dialog,padding=12);body.pack(fill='both',expand=True)
        columns=('account','summary_date','assets_date','domestic','foreign','cash','foreign_cash','usd_cash','growth','tsumitate')
        tree=ttk.Treeview(body,columns=columns,show='headings',height=6)
        labels=('口座','円サマリー取得日時','My資産取得日時','国内株式等（円）','外国株式（円）','円現金合計','外貨現金（円評価）','米ドル現金（ドル）','NISA成長枠','NISA積立枠')
        for column,label in zip(columns,labels):
            tree.heading(column,text=label);tree.column(column,width=170 if column.endswith('date') else 130,anchor='w')
        for account in self.store.accounts():
            summary=observations.get((account['id'],'口座サマリー'),{});assets=observations.get((account['id'],'My資産'),{})
            sv=summary.get('values',{});av=assets.get('values',{});amounts=combine_assets(av)
            cash=sv.get('cash_total') if sv.get('cash_total') is not None else amounts.get('cash')
            def amount(value):return '未取得' if value is None else f'{value:,.2f}'.rstrip('0').rstrip('.')
            limits=[sv.get(key,{}) for key in ('growth_remaining','tsumitate_remaining')]
            values=[account['label'],summary.get('observed_at','未取得')[:19],assets.get('observed_at','未取得')[:19],
                    amount(amounts['domestic']),amount(amounts['foreign']),amount(cash),amount(amounts['foreign_cash']),amount(av.get('usd_cash'))]
            values.extend(amount(value.get('remaining'))+(f"（{value['year']}年）" if 'year' in value else '') for value in limits)
            tree.insert('','end',values=values)
        horizontal=ttk.Scrollbar(body,orient='horizontal',command=tree.xview);tree.configure(xscrollcommand=horizontal.set)
        tree.pack(fill='both',expand=True);horizontal.pack(fill='x')
        ttk.Button(dialog,text='閉じる',command=dialog.destroy).pack(pady=(0,12))

    def record_assets(self):
        try:
            a=self.selected()
            if a['id']=='E':raise LedgerError('父の資産は管理Excelに手入力してください。')
            day=simpledialog.askstring('評価基準日','表示額の実際の基準日（YYYY-MM-DD）を入力。取得日を月末へ読み替えないでください。')
            if not day:return
            values={}
            for key,label in [('domestic','国内株式等の円評価'),('foreign','外国株式の円評価'),('cash','証券預り金＋連携銀行預金の実残高（円）'),('foreign_cash','外貨現金の円評価')]:
                v=simpledialog.askstring('資産額',label+'。不明は空欄、保有なしは0。買付余力を重ねて加算しないでください。')
                if v is None:return
                values[key]=v
            note=simpledialog.askstring('確認メモ','銀行預金を含む範囲・表示為替・基準時刻等を記録してください。')
            if note is None:return
            from asset_snapshots import save
            save(self.store,a['id'],day,values,note=note)
            messagebox.showinfo('記録しました','対象の月末・月初と一致すると、取引シート右側に表示されます。')
        except Exception as e:messagebox.showerror('記録できません',str(e))

    def export_excel(self):
        try:
            target=export_trades(self.store,BASE/'output',self.month.get().strip(),self.activities)
            messagebox.showinfo('売買記録を出力しました',f'{target}\n\n「取引」「配当」の2シートに出力しました。')
        except (LedgerError,OSError) as e:messagebox.showerror('出力できません',str(e))

    def close(self):
        if self.busy:messagebox.showinfo('更新中','更新画面から中止するか、処理の終了をお待ちください。');return
        self.store.close();self.root.destroy()

if __name__=='__main__':
    root=tk.Tk()
    try:App(root);root.mainloop()
    except LedgerError as error:messagebox.showerror('口座の移行を確認してください',str(error));root.destroy()
