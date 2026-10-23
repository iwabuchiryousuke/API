"""Read-only monthly comparisons and explicitly invoked Windows task actions.

Opening this window never registers a task or starts acquisition. A test runs
through monthly_runner's shared update lock, not a second UI-owned lock.
"""
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox,ttk
import webbrowser

from monthly_records import MonthlyRecordError,comparison
from monthly_schedule import JST,next_slots


_ACCOUNTS=tuple('ABCD')
_STATUSES={'completed':'取得処理完了','partial':'一部未完了','failed':'失敗',
           'cancelled':'中止','running':'実行中の記録','busy':'別の更新が実行中',
           'skipped':'対象外・未実行'}
_ASSETS={'complete':'株式・現金を取得済み','reused':'保存済みの完了記録を使用',
         'partial':'一部未取得','failed':'取得失敗','pending':'未処理',
         'needs-attention':'認証・設定の確認が必要'}
_PDFS={'not-requested':'対象外','complete':'保存済み','reused':'保存済み記録を使用',
       'failed':'取得失敗','blocked':'認証・設定の確認待ち'}
_PAIRS={'complete':'両日取得済み','late':'両日取得済み・遅延あり',
        'partial':'一部の金額が未取得','missing':'片日または両日の記録なし'}
_TIMING={'near-schedule':'16時後30分以内','late':'16時30分より後'}
_FIELDS={'summary_stocks':'株式表示額','cash_total':'現金合計'}
_MAX_REPORT_BYTES=2*1024*1024
_MONTH=re.compile(r'(?:19|[2-9][0-9])[0-9]{2}-(?:0[1-9]|1[0-2])\Z')

# Arguments and paths stay separate from this fixed code. There is no shell,
# and no password or account credential is passed through the command line.
# Keeping the console open lets the user read Task Scheduler's actual output.
_CONSOLE_LAUNCHER="""import runpy,sys
target=sys.argv.pop(1)
sys.argv[0]=target
code=0
try:
    runpy.run_path(target,run_name='__main__')
except SystemExit as error:
    code=error.code if isinstance(error.code,int) else 1
except KeyboardInterrupt:
    code=130
except Exception:
    print('操作を完了できませんでした。ローカルの実行状況を確認してください。')
    code=1
try:
    input('結果を確認し、Enterを押すとアプリに戻ります。')
except (EOFError,KeyboardInterrupt):
    pass
raise SystemExit(code)
"""


def display_stamp(value):
    """Display aware timestamps only; never print an arbitrary report string."""
    try:
        if not isinstance(value,str) or len(value)>64:raise ValueError()
        stamp=datetime.fromisoformat(value)
        if stamp.tzinfo is None or stamp.utcoffset() is None:raise ValueError()
        return stamp.astimezone(JST).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError,TypeError,OverflowError):return '未取得・時刻未確認'


def amount(value,delta=False):
    if value is None:return '未取得'
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):return '未確認'
    if isinstance(value,int):text=f'{value:,}'
    else:text=f'{value:,.2f}'.rstrip('0').rstrip('.')
    return ('+' if delta and value>0 else '')+text


def comparison_rows(value,profiles):
    """Format one coherent record per side; unknown amounts remain unknown."""
    rows=[]
    for account in _ACCOUNTS:
        profile=profiles.get(account,{})
        label=account+'：'+str(profile.get('label') or account)
        item=value.get('accounts',{}).get(account)
        if item is None:
            rows.append((label,'PDF名義の設定が必要',*(['未取得']*8),'—','—'))
            continue
        before=item.get('month_end');after=item.get('month_start')
        bv=before['summary']['values'] if before else {}
        av=after['summary']['values'] if after else {}
        times=item.get('observed_at',{});timing=item.get('timing',{});deltas=item.get('deltas',{})
        rows.append((label,_PAIRS.get(item.get('status'),'状態未確認'),
            display_stamp(times.get('month-end')),amount(bv.get('summary_stocks')),amount(bv.get('cash_total')),
            display_stamp(times.get('month-start')),amount(av.get('summary_stocks')),amount(av.get('cash_total')),
            amount(deltas.get('summary_stocks'),True),amount(deltas.get('cash_total'),True),
            _TIMING.get(timing.get('month-end'),'未取得'),_TIMING.get(timing.get('month-start'),'未取得')))
    return rows


def _regular_path(base,parts):
    """Never follow a link/junction from a report or CSV into another folder."""
    current=Path(base).absolute()
    for index,part in enumerate((None,)+tuple(parts)):
        if part is not None:current=current/part
        try:
            info=current.lstat()
        except FileNotFoundError:return None
        if (current.is_symlink() or getattr(info,'st_file_attributes',0)&
                getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0)):
            raise OSError('通常の保存先を使用してください。')
        final=index==len(parts)
        if final:
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise OSError('通常のファイルを使用してください。')
        elif not stat.S_ISDIR(info.st_mode):raise OSError('通常のフォルダーを使用してください。')
    return current


def _unique_pairs(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('duplicate key')
        result[key]=value
    return result


def read_run_status(base,test=False):
    """Return only known status enums/timestamps; raw errors and URLs stay local."""
    kind='テスト' if test else '本番'
    empty={'headline':kind+'：保存済みの実行記録はありません。','rows':[],'warning':None}
    try:
        path=_regular_path(base,('data','scheduler','latest_test_run.json' if test else 'latest_run.json'))
        if path is None:return empty
        if path.stat().st_size>_MAX_REPORT_BYTES:raise ValueError()
        with path.open('rb') as handle:raw=handle.read(_MAX_REPORT_BYTES+1)
        if len(raw)>_MAX_REPORT_BYTES:raise ValueError()
        value=json.loads(raw.decode('utf-8'),object_pairs_hook=_unique_pairs)
        if not isinstance(value,dict) or not isinstance(value.get('accounts'),dict):raise ValueError()
        if value.get('test') is not test:raise ValueError()
        status=_STATUSES.get(value.get('status'),'状態未確認')
        headline=(kind+'：'+status+' ／ 開始 '+display_stamp(value.get('started_at'))+
                  ' ／ 記録更新 '+display_stamp(value.get('finished_at')))
        rows=[]
        for account in _ACCOUNTS:
            item=value['accounts'].get(account,{})
            if not isinstance(item,dict):raise ValueError()
            missing=item.get('missing')
            if missing is None:gaps='—'
            elif isinstance(missing,list) and all(key in _FIELDS for key in missing):
                gaps='・'.join(_FIELDS[key] for key in missing) or 'なし（両方取得）'
            else:gaps='未取得項目を確認できません'
            note='ローカル診断を確認' if item.get('error') or item.get('pdf_error') else ''
            rows.append((kind,account,_ASSETS.get(item.get('asset_status'),'記録なし'),
                _PDFS.get(item.get('pdf_status'),'記録なし'),display_stamp(item.get('observed_at')),gaps,note))
        return {'headline':headline,'rows':rows,'warning':None}
    except (OSError,ValueError,TypeError,RecursionError):
        empty['headline']=kind+'：保存済みの実行記録を確認できません。'
        empty['warning']='実行記録の形式・保存先を確認してください。任意のファイル内容や認証情報は表示していません。'
        return empty


def _table(parent,headers,widths):
    frame=ttk.Frame(parent);frame.pack(fill='both',expand=True,padx=8,pady=8)
    columns=tuple(str(index) for index in range(len(headers)))
    tree=ttk.Treeview(frame,columns=columns,show='headings',selectmode='browse')
    for column,label,width in zip(columns,headers,widths):
        tree.heading(column,text=label);tree.column(column,width=width,stretch=False,anchor='w')
    y=ttk.Scrollbar(frame,orient='vertical',command=tree.yview)
    x=ttk.Scrollbar(frame,orient='horizontal',command=tree.xview)
    tree.configure(yscrollcommand=y.set,xscrollcommand=x.set)
    tree.grid(row=0,column=0,sticky='nsew');y.grid(row=0,column=1,sticky='ns');x.grid(row=1,column=0,sticky='ew')
    frame.rowconfigure(0,weight=1);frame.columnconfigure(0,weight=1)
    return tree


class MonthlyDialog:
    def __init__(self,app,base=None):
        self.app=app;self.base=Path(base or Path(__file__).resolve().parent)
        self.process=None;self.controls=[];self.saved_button_states=[];self.value={'accounts':{}}
        self.dialog=tk.Toplevel(app.root);self.dialog.title('月末・月初16時の取得と比較')
        self.dialog.geometry('1240x750');self.dialog.minsize(950,620);self.dialog.transient(app.root)
        self.dialog.protocol('WM_DELETE_WINDOW',self.close)
        outer=ttk.Frame(self.dialog,padding=14);outer.pack(fill='both',expand=True)
        ttk.Label(outer,text='月末と翌月1日の、口座サマリーの株式表示額・現金合計を比較します。',font=('Yu Gothic UI',12,'bold')).pack(anchor='w')
        ttk.Label(outer,text='対象はA～D。株式には円建の口座サマリー表示額を使用します。差額は運用損益ではありません。',wraplength=1160).pack(anchor='w',pady=(4,0))
        self.next_text=tk.StringVar();ttk.Label(outer,textvariable=self.next_text,wraplength=1160).pack(anchor='w',pady=(8,6))

        tasks=ttk.LabelFrame(outer,text='Windowsの自動実行（ボタンを押すまで登録・取得しません）',padding=10)
        tasks.pack(fill='x',pady=(0,8))
        modes=ttk.Frame(tasks);modes.pack(fill='x')
        ttk.Label(modes,text='実行方式').pack(side='left')
        self.logon=tk.StringVar(value='ログオン中（標準）')
        mode=ttk.Combobox(modes,textvariable=self.logon,state='readonly',width=36,
                         values=('ログオン中（標準）','サインアウト中も実行（パスワード）'))
        mode.pack(side='left',padx=8);self.controls.append(mode)
        for text,action in (('登録・更新','install'),('登録状態を確認','status'),('テスト取得（PDFなし）','test'),('登録を解除','uninstall')):
            button=ttk.Button(modes,text=text,command=lambda action=action:self.run_action(action))
            button.pack(side='left',padx=3);self.controls.append(button)
        ttk.Label(tasks,text='同じWindowsユーザー・日本のタイムゾーンで使用します。標準はログオンが必要です。PC電源OFFでは実行できません。',wraplength=1140).pack(anchor='w',pady=(6,0))
        ttk.Label(tasks,text='パスワード方式はWindowsの安全な入力画面を使います（PINではありません）。画面ロック・復帰・無人の追加認証は実機で確認してください。',wraplength=1140).pack(anchor='w')
        self.action_text=tk.StringVar(value='操作結果は別のコンソールで確認し、Enterを押すと戻ります。テスト中はアプリの他の更新を止めます。')
        ttk.Label(tasks,textvariable=self.action_text,wraplength=1140,foreground='#6b5000').pack(anchor='w',pady=(6,0))

        chooser=ttk.Frame(outer);chooser.pack(fill='x')
        ttk.Label(chooser,text='比較月（YYYY-MM）').pack(side='left')
        self.month=tk.StringVar(value=datetime.now(JST).strftime('%Y-%m'))
        entry=ttk.Entry(chooser,textvariable=self.month,width=10);entry.pack(side='left',padx=8);self.controls.append(entry)
        entry.bind('<Return>',lambda _:self.refresh())
        for text,command in (('前月',lambda:self.move_month(-1)),('翌月',lambda:self.move_month(1)),('表示を更新',self.refresh)):
            button=ttk.Button(chooser,text=text,command=command);button.pack(side='left',padx=3);self.controls.append(button)
        self.csv_button=ttk.Button(chooser,text='保存済み比較CSVを開く',command=self.open_csv);self.csv_button.pack(side='left',padx=10);self.controls.append(self.csv_button)
        self.warning=tk.StringVar();ttk.Label(outer,textvariable=self.warning,foreground='#955400',wraplength=1160).pack(anchor='w',pady=(6,2))

        book=ttk.Notebook(outer);book.pack(fill='both',expand=True,pady=(4,0))
        pairs=ttk.Frame(book);runs=ttk.Frame(book);book.add(pairs,text='月末・1日の比較');book.add(runs,text='保存済みの実行状況')
        ttk.Label(pairs,text='比較月の前月末16時以降 → 当月1日16時以降の実取得値です。16:00ちょうどの評価額・終値を保証しません。',wraplength=1140,padding=8).pack(anchor='w')
        self.pair_tree=_table(pairs,('口座','判定','前月末・実取得（JST）','前月末・株式（円）','前月末・現金（円）',
            '当月1日・実取得（JST）','当月1日・株式（円）','当月1日・現金（円）','株式の増減（円）','現金の増減（円）','月末の時刻区分','1日の時刻区分'),
            (180,220,185,150,150,185,150,150,150,150,160,160))
        self.details=tk.StringVar(value='口座の行を選ぶと、予定時刻・現金内訳・任意のNISA枠を確認できます。')
        ttk.Label(pairs,textvariable=self.details,wraplength=1130,padding=(8,0,8,8)).pack(anchor='w')
        self.pair_tree.bind('<<TreeviewSelect>>',self.select_row)
        self.run_headline=tk.StringVar();ttk.Label(runs,textvariable=self.run_headline,wraplength=1140,padding=8).pack(anchor='w')
        ttk.Label(runs,text='本番とテストを区別します。保存済みレポートは現在の稼働を証明せず、タスクの結果コード0もSBI取得成功の保証ではありません。',wraplength=1140,padding=(8,0)).pack(anchor='w')
        self.run_tree=_table(runs,('種別','口座','株式・現金','PDF','実取得（JST）','主要2項目の不足','確認'),(80,70,230,180,185,230,230))
        ttk.Label(outer,text='自動処理未実行・PC電源OFFなどで逃した過去日の金額は復元しません。比較CSVは別出力です。既存の売買記録Excelの形式は変更しません。',wraplength=1160).pack(anchor='w',pady=(8,4))
        ttk.Button(outer,text='閉じる',command=self.close).pack(anchor='e')
        self.refresh()

    def valid_month(self):
        month=self.month.get().strip()
        if not _MONTH.fullmatch(month):
            messagebox.showerror('比較月','比較月をYYYY-MM（1900年以降）で入力してください。',parent=self.dialog)
            return None
        return month

    def move_month(self,offset):
        if self.app.busy:return
        month=self.valid_month()
        if month is None:return
        year,number=map(int,month.split('-'));index=year*12+number-1+offset
        year,number=divmod(index,12)
        if 1900<=year<=9999:self.month.set(f'{year:04d}-{number+1:02d}');self.refresh()

    def refresh(self):
        if self.app.busy:return
        month=self.valid_month()
        if month is None:return
        slots=next_slots(datetime.now(JST),2)
        self.next_text.set('次の予定：'+' ／ '.join(display_stamp(slot['scheduled_for'])+
            (' 月末・株式/現金' if slot['role']=='month-end' else ' 1日・株式/現金＋PDF保存') for slot in slots)+'（日本時間）')
        warnings=[]
        self.profiles={row['id']:row for row in self.app.store.accounts() if row['id'] in _ACCOUNTS}
        owners={key:profile.get('owner_name') for key,profile in self.profiles.items() if profile.get('owner_name')}
        self.comparison_valid=False
        try:
            self.value=comparison(self.base,month,owners)
            self.comparison_valid=set(owners)==set(_ACCOUNTS)
        except (MonthlyRecordError,OSError,ValueError):
            self.value={'accounts':{}}
            warnings.append('月次記録の名義・整合性を確認できません。金額を推定せず表示を止めています。保存先と現在のPDF名義を確認してください。')
        missing=[key for key in _ACCOUNTS if key not in owners]
        if missing:warnings.append('口座'+ '・'.join(missing)+'のPDF名義を設定してください。')
        for tree in (self.pair_tree,self.run_tree):
            for item in tree.get_children():tree.delete(item)
        for account,row in zip(_ACCOUNTS,comparison_rows(self.value,self.profiles)):
            if owners.get(account) and account not in self.value['accounts']:
                row=(row[0],'記録の確認が必要',*row[2:])
            self.pair_tree.insert('','end',iid=account,values=row)
        reports=[read_run_status(self.base,test) for test in (False,True)]
        self.run_headline.set('\n'.join(report['headline'] for report in reports))
        for report in reports:
            if report['warning']:warnings.append(report['warning'])
            for row in report['rows']:self.run_tree.insert('','end',values=row)
        self.warning.set('\n'.join(dict.fromkeys(warnings)))
        self.details.set('未取得は0円として扱いません。任意のNISA枠は読めた場合だけ、選択した行の詳細に表示します。')
        try:path=_regular_path(self.base,('output','monthly',month+'_資産比較.csv'))
        except OSError:path=None
        self.csv_button.configure(state='normal' if path is not None and self.comparison_valid else 'disabled')

    def select_row(self,_event=None):
        selection=self.pair_tree.selection()
        if not selection:return
        item=self.value.get('accounts',{}).get(selection[0])
        if item is None:self.details.set('名義または保存済み記録を確認できないため、金額は表示していません。');return
        lines=[]
        for role,key,label in (('month-end','month_end','月末'),('month-start','month_start','1日')):
            record=item[key]
            line=label+'：予定 '+display_stamp(item['scheduled_for'][role])
            if record is None:lines.append(line+' ／ 記録なし');continue
            values=record['summary']['values']
            line+=' ／ 証券預り金 '+amount(values.get('broker_cash'))+'円・銀行等 '+amount(values.get('bank'))+'円'
            for name,title in (('growth_remaining','NISA成長枠'),('tsumitate_remaining','NISAつみたて枠')):
                nisa=values.get(name)
                if nisa is not None:line+=' ／ '+title+' '+str(nisa['year'])+'年 '+amount(nisa.get('remaining'))+'円'
            if record['summary']['displayed_at']:line+=' ／ 画面時計 '+record['summary']['displayed_at']+'（評価基準時刻とは未確認）'
            lines.append(line)
        self.details.set('\n'.join(lines))

    def open_csv(self):
        if self.app.busy:return
        month=self.valid_month()
        if month is None:return
        # Settings or the entry's month can change while this window is open.
        # Revalidate current owners before exposing an older saved artifact.
        self.refresh()
        if not getattr(self,'comparison_valid',False):
            messagebox.showinfo('比較CSV','A～Dの現在の名義と記録の整合性を確認してから、保存済みCSVを開いてください。',parent=self.dialog);return
        try:
            path=_regular_path(self.base,('output','monthly',month+'_資産比較.csv'))
            if path is None:
                messagebox.showinfo('比較CSV','この月のCSVはまだありません。本番取得で比較結果を保存すると作成されます。',parent=self.dialog)
                return
            if os.name=='nt':os.startfile(str(path))
            else:webbrowser.open(path.resolve().as_uri())
        except OSError:messagebox.showerror('比較CSV','CSVを開けません。通常の出力先と関連付けアプリを確認してください。',parent=self.dialog)

    def _set_busy(self):
        self.app.busy=True;self.saved_button_states=[]
        for widget in list(self.app.buttons)+self.controls:
            try:
                self.saved_button_states.append((widget,widget.cget('state')))
                widget.configure(state='disabled')
            except tk.TclError:pass

    def _restore(self):
        self.app.busy=False
        for widget,state in self.saved_button_states:
            try:widget.configure(state=state)
            except tk.TclError:pass
        self.saved_button_states=[]

    def run_action(self,action):
        if action not in ('install','status','test','uninstall'):return
        if self.app.busy:
            messagebox.showinfo('実行中','現在の処理が終わるまでお待ちください。',parent=self.dialog);return
        if os.name!='nt':
            messagebox.showinfo('Windowsで実行','SBI・Gmailを設定した同じWindowsユーザーで実行してください。この環境では登録・実取得を行いません。',parent=self.dialog);return
        logon='Password' if self.logon.get()=='サインアウト中も実行（パスワード）' else 'InteractiveToken'
        argv=[sys.executable,'-c',_CONSOLE_LAUNCHER,str(self.base/'windows_tasks.py'),'--action',action,
              '--logon-mode',logon,'--role','month-end']
        self._set_busy()
        try:
            self.process=subprocess.Popen(argv,cwd=str(self.base),creationflags=getattr(subprocess,'CREATE_NEW_CONSOLE',0))
        except OSError:
            self.process=None;self._restore()
            self.action_text.set('操作を起動できません。Windows・Python・アプリの配置先を確認してください。');return
        name={'install':'タスク登録','status':'登録状態の確認','test':'テスト取得（PDFなし）','uninstall':'登録解除'}[action]
        self.action_text.set(name+'を別画面で実行しています。結果を確認し Enter を押すまで、このアプリの他の操作は停止します。')
        self.app.root.after(500,lambda:self.poll_action(name))

    def poll_action(self,name):
        if self.process is None:return
        code=self.process.poll()
        if code is None:
            self.app.root.after(500,lambda:self.poll_action(name));return
        self.process=None;self._restore()
        self.action_text.set(name+'が終了しました（終了コード '+str(code)+'）。保存済みレポートの取得日時と不足項目も確認してください。')
        self.refresh()

    def close(self):
        if self.process is not None:
            messagebox.showinfo('実行中','別画面の処理を完了し、結果確認後に Enter を押してください。処理中はアプリを閉じられません。',parent=self.dialog);return
        if getattr(self.app,'_monthly_dialog',None) is self:self.app._monthly_dialog=None
        self.dialog.destroy()


def show(app,base=None):
    """App entrypoint: ``from monthly_dialog import show; show(self, BASE)``."""
    if app.busy:
        messagebox.showinfo('実行中','現在の処理が終わるまでお待ちください。',parent=app.root);return None
    existing=getattr(app,'_monthly_dialog',None)
    if existing is not None:
        try:
            if existing.dialog.winfo_exists():existing.dialog.lift();existing.dialog.focus_set();return existing
        except tk.TclError:pass
    dialog=MonthlyDialog(app,base);app._monthly_dialog=dialog
    return dialog
