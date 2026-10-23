"""Reservation management, monthly comparisons and Windows task actions.

Opening this window never registers a task or starts acquisition. A test runs
through monthly_runner's shared update lock, not a second UI-owned lock.
"""
from calendar import monthrange
from datetime import datetime,timedelta
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
import unicodedata
import webbrowser

from monthly_records import MonthlyRecordError,comparison
from monthly_schedule import JST


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
_TIMING={'near-schedule':'予定時刻後30分以内','late':'予定時刻から30分より後'}
_FIELDS={'summary_stocks':'株式表示額','cash_total':'現金合計'}
_MAX_REPORT_BYTES=2*1024*1024
_MONTH=re.compile(r'(?:19|[2-9][0-9])[0-9]{2}-(?:0[1-9]|1[0-2])\Z')
_SCHEDULE_KINDS={'month-end':'毎月末日','month-start':'毎月1日','once':'指定日時に1回'}


def schedule_rows(settings):
    """Format validated settings without turning disabled entries into jobs."""
    rows=[]
    for item in settings['schedules']:
        if item['deleted']:continue
        planned=display_stamp(item['run_at']) if item['kind']=='once' else _SCHEDULE_KINDS[item['kind']]+' '+item['time']
        rows.append((item['id'],(item['label'],_SCHEDULE_KINDS[item['kind']],planned,
            '資産＋PDF保存' if item['include_pdfs'] else '資産のみ','有効' if item['enabled'] else '無効')))
    return rows


def upcoming_reservations(settings,now=None,count=3):
    """Preview enabled calendar settings; do not fabricate missed executions."""
    current=(now or datetime.now(JST)).astimezone(JST)
    upcoming=[]
    for item in settings['schedules']:
        if item['deleted'] or not item['enabled']:continue
        if item['kind']=='once':
            stamp=datetime.fromisoformat(item['run_at']).astimezone(JST)
            if stamp>=current:upcoming.append((stamp,item['label'],item['include_pdfs']))
            continue
        hour,minute=map(int,item['time'].split(':'))
        for offset in range(3):
            year,month=divmod(current.year*12+current.month-1+offset,12);month+=1
            if year>9999:break
            day=monthrange(year,month)[1] if item['kind']=='month-end' else 1
            stamp=datetime(year,month,day,hour,minute,tzinfo=JST)
            if stamp>=current:
                upcoming.append((stamp,item['label'],item['include_pdfs']));break
    return sorted(upcoming,key=lambda value:(value[0],value[1]))[:count]


def edit_schedule_value(original,label,day,clock,include_pdfs,enabled,now=None):
    """Validate editor input before settings or Windows tasks can change."""
    value=dict(original or {'kind':'once'})
    name=label.strip()
    if not name or len(name)>80 or any(unicodedata.category(char) in ('Cc','Cf') for char in name):
        raise ValueError('予約名を1～80文字で入力してください。')
    if not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]',clock):
        raise ValueError('時刻をHH:MM（00:00～23:59）で入力してください。')
    value.update(label=name,time=clock,include_pdfs=bool(include_pdfs),enabled=bool(enabled),deleted=False)
    if value['kind']=='once':
        try:
            stamp=datetime.strptime(day+' '+clock,'%Y-%m-%d %H:%M').replace(tzinfo=JST)
            if stamp.strftime('%Y-%m-%d')!=day:raise ValueError()
        except (ValueError,TypeError):raise ValueError('実在する日付をYYYY-MM-DDで入力してください。') from None
        current=now or datetime.now(JST)
        if current.tzinfo is None:raise ValueError('日本時間を確認できません。')
        if enabled and stamp<=current.astimezone(JST):raise ValueError('これから実行する日時を指定してください。')
        value['run_at']=stamp.isoformat()
    else:value['run_at']=None
    return value

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


def report_error_hint(value,prefix=''):
    """Only display fixed public error hints, never arbitrary report errors."""
    code=value.get(prefix+'error_code')
    try:
        from monthly_runner import RUN_ERROR_HINTS
        hint=RUN_ERROR_HINTS.get(code) if isinstance(code,str) else None
    except (ImportError,AttributeError):hint=None
    return hint if isinstance(hint,str) else 'ローカル診断を確認' if value.get(prefix+'error') else ''


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
            note=' ／ '.join(dict.fromkeys(hint for hint in
                (report_error_hint(item),report_error_hint(item,'pdf_')) if hint))
            rows.append((kind,account,_ASSETS.get(item.get('asset_status'),'記録なし'),
                _PDFS.get(item.get('pdf_status'),'記録なし'),display_stamp(item.get('observed_at')),gaps,note))
        hint=report_error_hint(value)
        return {'headline':headline,'rows':rows,'warning':hint or None}
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
        self.schedules={};self.settings_revision=None;self.logon_initialized=False
        self.dialog=tk.Toplevel(app.root);self.dialog.title('自動取得の予約・月次比較')
        self.dialog.geometry('1240x800');self.dialog.minsize(950,660);self.dialog.transient(app.root)
        self.dialog.protocol('WM_DELETE_WINDOW',self.close)
        outer=ttk.Frame(self.dialog,padding=14);outer.pack(fill='both',expand=True)
        ttk.Label(outer,text='自動取得の予約と、月末・月初の資産比較',font=('Yu Gothic UI',12,'bold')).pack(anchor='w')
        ttk.Label(outer,text='対象はA～D。株式には円建の口座サマリー表示額を使用します。差額は運用損益ではありません。',wraplength=1160).pack(anchor='w',pady=(4,0))
        self.next_text=tk.StringVar();ttk.Label(outer,textvariable=self.next_text,wraplength=1160).pack(anchor='w',pady=(8,6))

        tasks=ttk.LabelFrame(outer,text='Windowsへの反映・今すぐテスト',padding=10)
        tasks.pack(fill='x',pady=(0,8))
        modes=ttk.Frame(tasks);modes.pack(fill='x')
        ttk.Label(modes,text='実行方式').pack(side='left')
        self.logon=tk.StringVar(value='ログオン中（標準）')
        mode=ttk.Combobox(modes,textvariable=self.logon,state='readonly',width=36,
                         values=('ログオン中（標準）','サインアウト中も実行（パスワード）'))
        mode.pack(side='left',padx=8);self.controls.append(mode)
        for text,action in (('Windows予約を反映','sync'),('登録状態を確認','status'),('今すぐテスト取得','test')):
            button=ttk.Button(modes,text=text,command=lambda action=action:self.run_action(action))
            button.pack(side='left',padx=3);self.controls.append(button)
        testing=ttk.Frame(tasks);testing.pack(fill='x',pady=(6,0))
        self.test_show_browser=tk.BooleanVar(value=True);self.test_include_pdfs=tk.BooleanVar(value=False)
        for text,variable in (('テスト中のブラウザーを表示',self.test_show_browser),('テストでPDFも取得・保存',self.test_include_pdfs)):
            control=ttk.Checkbutton(testing,text=text,variable=variable);control.pack(side='left',padx=(0,18));self.controls.append(control)
        ttk.Label(tasks,text='テストは現在値を確認します。月末・月初の本番記録にはしません。',wraplength=1140).pack(anchor='w',pady=(4,0))
        ttk.Label(tasks,text='同じWindowsユーザー・日本のタイムゾーンで使用します。標準はログオンが必要です。PC電源OFFでは実行できません。',wraplength=1140).pack(anchor='w',pady=(6,0))
        ttk.Label(tasks,text='パスワード方式はWindowsの安全な入力画面を使います（PINではありません）。画面ロック・復帰・無人の追加認証は実機で確認してください。',wraplength=1140).pack(anchor='w')
        self.action_text=tk.StringVar(value='操作結果は別のコンソールで確認し、Enterを押すと戻ります。テスト中はアプリの他の更新を止めます。')
        ttk.Label(tasks,textvariable=self.action_text,wraplength=1140,foreground='#6b5000').pack(anchor='w',pady=(6,0))

        self.warning=tk.StringVar();ttk.Label(outer,textvariable=self.warning,foreground='#955400',wraplength=1160).pack(anchor='w',pady=(6,2))
        book=ttk.Notebook(outer);book.pack(fill='both',expand=True,pady=(4,0))
        reservations=ttk.Frame(book);pairs=ttk.Frame(book);runs=ttk.Frame(book)
        book.add(reservations,text='取得日時の予約');book.add(pairs,text='月末・1日の比較');book.add(runs,text='保存済みの実行状況')
        self._build_reservations(reservations)

        chooser=ttk.Frame(pairs,padding=8);chooser.pack(fill='x')
        ttk.Label(chooser,text='比較月（YYYY-MM）').pack(side='left')
        self.month=tk.StringVar(value=datetime.now(JST).strftime('%Y-%m'))
        entry=ttk.Entry(chooser,textvariable=self.month,width=10);entry.pack(side='left',padx=8);self.controls.append(entry)
        entry.bind('<Return>',lambda _:self.refresh())
        for text,command in (('前月',lambda:self.move_month(-1)),('翌月',lambda:self.move_month(1)),('表示を更新',self.refresh)):
            button=ttk.Button(chooser,text=text,command=command);button.pack(side='left',padx=3);self.controls.append(button)
        self.csv_button=ttk.Button(chooser,text='保存済み比較CSVを開く',command=self.open_csv);self.csv_button.pack(side='left',padx=10);self.controls.append(self.csv_button)
        ttk.Label(pairs,text='前月末 → 当月1日の実取得値を比較します。予約した時刻と実取得時刻を保持し、16:00ちょうどの確定残高とは断定しません。',wraplength=1140,padding=8).pack(anchor='w')
        self.details=tk.StringVar(value='口座の行を選ぶと、予定時刻・現金内訳・任意のNISA枠を確認できます。')
        ttk.Label(pairs,textvariable=self.details,wraplength=1130,padding=(8,0,8,8)).pack(side='bottom',anchor='w')
        self.pair_tree=_table(pairs,('口座','判定','前月末・実取得（JST）','前月末・株式（円）','前月末・現金（円）',
            '当月1日・実取得（JST）','当月1日・株式（円）','当月1日・現金（円）','株式の増減（円）','現金の増減（円）','月末の時刻区分','1日の時刻区分'),
            (180,220,185,150,150,185,150,150,150,150,160,160))
        self.pair_tree.bind('<<TreeviewSelect>>',self.select_row)
        self.run_headline=tk.StringVar();ttk.Label(runs,textvariable=self.run_headline,wraplength=1140,padding=8).pack(anchor='w')
        ttk.Label(runs,text='本番とテストを区別します。保存済みレポートは現在の稼働を証明せず、タスクの結果コード0もSBI取得成功の保証ではありません。',wraplength=1140,padding=(8,0)).pack(anchor='w')
        self.run_details=tk.StringVar(value='取得に失敗した行を選ぶと、理由と次に確認することを表示します。')
        ttk.Label(runs,textvariable=self.run_details,wraplength=1130,padding=(8,0,8,8)).pack(side='bottom',anchor='w')
        self.run_tree=_table(runs,('種別','口座','株式・現金','PDF','理由・次に確認すること','実取得（JST）','主要2項目の不足'),(70,60,180,130,440,185,220))
        self.run_tree.bind('<<TreeviewSelect>>',self.select_run_row)
        ttk.Label(outer,text='自動処理未実行・PC電源OFFなどで逃した過去日の金額は復元しません。比較CSVは別出力です。既存の売買記録Excelの形式は変更しません。',wraplength=1160).pack(anchor='w',pady=(8,4))
        ttk.Button(outer,text='閉じる',command=self.close).pack(anchor='e')
        self.refresh()

    def _build_reservations(self,parent):
        ttk.Label(parent,text='標準の「毎月末日16:00」「毎月1日16:00」に加えて、指定日時の無人取得を予約できます。',wraplength=1140,padding=(8,8,8,0)).pack(anchor='w')
        ttk.Label(parent,text='予約の編集・無効化・削除は保存されます。削除した標準予約を次の起動で勝手に作り直しません。',wraplength=1140,padding=(8,2,8,0)).pack(anchor='w')
        self.registration_text=tk.StringVar()
        ttk.Label(parent,textvariable=self.registration_text,wraplength=1140,padding=8).pack(anchor='w')
        bar=ttk.Frame(parent,padding=8);bar.pack(fill='x')
        for text,command in (('日時を追加',lambda:self.edit_reservation(True)),
            ('選んだ予約を変更',self.edit_reservation),('有効／無効を切替',self.toggle_reservation),
            ('選んだ予約を削除',self.delete_reservation),('標準の2予約を復元',self.restore_reservations)):
            button=ttk.Button(bar,text=text,command=command);button.pack(side='left',padx=3);self.controls.append(button)
        ttk.Label(parent,text='保存するとWindowsへの反映を開始します。反映に失敗した場合、設定は保持されるので「Windows予約を反映」から再試行してください。',wraplength=1140,padding=(8,0,8,8)).pack(anchor='w')
        self.schedule_tree=_table(parent,('予約名','種類','日時（日本時間）','取得する内容','設定'),(270,140,230,160,100))
        self.schedule_tree.bind('<Double-1>',lambda _:self.edit_reservation())

    def refresh_reservations(self):
        from schedule_settings import load_settings,read_registration_status
        try:
            settings=load_settings(self.base,create=False)
            registration=read_registration_status(self.base)
            if not self.logon_initialized:
                if registration and registration.get('logon_mode')=='Password':
                    self.logon.set('サインアウト中も実行（パスワード）')
                self.logon_initialized=True
            self.schedules={item['id']:item for item in settings['schedules']}
            self.settings_revision=settings['revision']
            for row in self.schedule_tree.get_children():self.schedule_tree.delete(row)
            for identifier,values in schedule_rows(settings):self.schedule_tree.insert('','end',iid=identifier,values=values)
            planned=upcoming_reservations(settings)
            self.next_text.set('次の有効な予約：'+(' ／ '.join(
                stamp.strftime('%Y-%m-%d %H:%M')+' '+label+('（資産＋PDF）' if pdfs else '（資産のみ）')
                for stamp,label,pdfs in planned) if planned else 'ありません')+'（日本時間）')
            if registration is None:
                status='Windowsへの反映記録がありません。「Windows予約を反映」で登録できます。'
            elif registration['status']=='registered' and registration['settings_revision']==settings['revision']:
                status='前回のWindows反映は完了（'+display_stamp(registration.get('updated_at'))+'）。現在の登録は「登録状態を確認」で確認してください。'
            elif registration['status']=='failed':
                status='設定は保存済みですが、前回のWindows反映に失敗しました。「Windows予約を反映」で再試行してください。'
            else:status='設定の変更をWindowsへ反映する必要があります。「Windows予約を反映」を押してください。'
            self.registration_text.set(status)
            return []
        except (OSError,ValueError,TypeError):
            self.schedules={};self.settings_revision=None
            for row in self.schedule_tree.get_children():self.schedule_tree.delete(row)
            self.next_text.set('予約の設定を確認できません。')
            self.registration_text.set('設定ファイルの形式・保存先を確認してください。設定を推定して上書きしません。')
            return ['予約の設定を読み取れません。保存先・同時編集・設定の整合性を確認してください。']

    def selected_reservation(self):
        selection=self.schedule_tree.selection()
        item=self.schedules.get(selection[0]) if selection else None
        if item is None:
            messagebox.showinfo('予約を選択','変更する予約の行を選択してください。',parent=self.dialog)
        return item

    def save_reservation(self,value,original=None):
        """Persist a validated edit, then explicitly apply it to Windows."""
        from schedule_settings import save_schedule
        saved=save_schedule(self.base,value,expected_revision=original['revision'] if original else None)
        self.refresh()
        self.action_text.set('予約設定を保存しました。Windowsへの反映を開始します。')
        self.run_action('sync')
        return saved

    def edit_reservation(self,new=False):
        if self.app.busy:return
        original=None if new else self.selected_reservation()
        if not new and original is None:return
        future=datetime.now(JST).replace(second=0,microsecond=0)+timedelta(minutes=30)
        item=original or {'kind':'once','label':'日時指定の取得','time':future.strftime('%H:%M'),
                          'run_at':future.isoformat(),'include_pdfs':True,'enabled':True}
        editor=tk.Toplevel(self.dialog);editor.title('日時指定の予約を追加' if new else '予約を変更')
        editor.transient(self.dialog);editor.resizable(False,False)
        panel=ttk.Frame(editor,padding=18);panel.pack(fill='both',expand=True)
        ttk.Label(panel,text=_SCHEDULE_KINDS[item['kind']]+'（日本時間）',font=('Yu Gothic UI',12,'bold')).grid(row=0,column=0,columnspan=2,sticky='w',pady=(0,12))
        label=tk.StringVar(value=item['label']);day=tk.StringVar(value=item['run_at'][:10] if item['run_at'] else future.strftime('%Y-%m-%d'))
        clock=tk.StringVar(value=item['time']);pdfs=tk.BooleanVar(value=item['include_pdfs']);enabled=tk.BooleanVar(value=item['enabled'])
        ttk.Label(panel,text='予約名').grid(row=1,column=0,sticky='w',pady=6)
        name_entry=ttk.Entry(panel,textvariable=label,width=38);name_entry.grid(row=1,column=1,sticky='ew',pady=6)
        ttk.Label(panel,text='日付（YYYY-MM-DD）').grid(row=2,column=0,sticky='w',padx=(0,14),pady=6)
        date_entry=ttk.Entry(panel,textvariable=day,width=16);date_entry.grid(row=2,column=1,sticky='w',pady=6)
        if item['kind']!='once':date_entry.configure(state='disabled')
        ttk.Label(panel,text='時刻（HH:MM）').grid(row=3,column=0,sticky='w',pady=6)
        ttk.Entry(panel,textvariable=clock,width=10).grid(row=3,column=1,sticky='w',pady=6)
        ttk.Checkbutton(panel,text='資産取得後にPDFも取得・保存する',variable=pdfs).grid(row=4,column=0,columnspan=2,sticky='w',pady=6)
        ttk.Checkbutton(panel,text='この予約を有効にする',variable=enabled).grid(row=5,column=0,columnspan=2,sticky='w',pady=6)
        ttk.Label(panel,text='PDFの取引台帳への反映は、後で全口座一括確認から行います。\nPC電源OFF中は実行できません。',wraplength=480).grid(row=6,column=0,columnspan=2,sticky='w',pady=(6,12))
        buttons=ttk.Frame(panel);buttons.grid(row=7,column=0,columnspan=2,sticky='e')
        def save():
            try:
                value=edit_schedule_value(original,label.get(),day.get().strip(),clock.get().strip(),pdfs.get(),enabled.get())
                from schedule_settings import save_schedule
                save_schedule(self.base,value,expected_revision=original['revision'] if original else None)
            except ValueError as error:
                text=str(error)
                if len(text)>300 or '://' in text:text='日付・時刻・予約名・同時編集の有無を確認してください。'
                messagebox.showerror('予約を保存できません',text,parent=editor);return
            except OSError:
                messagebox.showerror('予約を保存できません','保存先のアクセス権と空き容量を確認してください。既存設定は保持しています。',parent=editor);return
            editor.grab_release();editor.destroy();self.refresh()
            self.action_text.set('予約設定を保存しました。Windowsへの反映を開始します。')
            self.run_action('sync')
        ttk.Button(buttons,text='キャンセル',command=editor.destroy).pack(side='left',padx=4)
        ttk.Button(buttons,text='保存してWindows予約を反映',command=save).pack(side='left',padx=4)
        editor.grab_set();name_entry.focus_set()

    def toggle_reservation(self):
        if self.app.busy:return
        original=self.selected_reservation()
        if original is None:return
        value=dict(original);value['enabled']=not value['enabled']
        if value['enabled'] and value['kind']=='once' and datetime.fromisoformat(value['run_at']).astimezone(JST)<=datetime.now(JST):
            messagebox.showinfo('日時を変更してください','この予約の日時は過ぎています。「選んだ予約を変更」から、これからの実行日時を指定してください。',parent=self.dialog);return
        try:self.save_reservation(value,original)
        except (OSError,ValueError):
            messagebox.showerror('予約を変更できません','保存先・同時編集・予約内容を確認してください。既存設定を推定して上書きしません。',parent=self.dialog)

    def delete_reservation(self):
        if self.app.busy:return
        item=self.selected_reservation()
        if item is None:return
        if not messagebox.askyesno('予約を削除','選んだ予約を削除します。取得済みのPDF・台帳・資産記録は残します。\n標準予約も次の起動で自動復元しません。',parent=self.dialog):return
        try:
            from schedule_settings import delete_schedule
            delete_schedule(self.base,item['id'])
            self.refresh();self.action_text.set('予約設定から削除しました。Windowsへの反映を開始します。');self.run_action('sync')
        except (OSError,ValueError):
            messagebox.showerror('予約を削除できません','予約設定の保存先と同時編集の有無を確認してください。',parent=self.dialog)

    def restore_reservations(self):
        if self.app.busy:return
        if not messagebox.askyesno('標準予約を復元','毎月末日16:00（資産）と毎月1日16:00（資産＋PDF）を標準設定に戻します。\n追加した日時予約は保持します。',parent=self.dialog):return
        try:
            from schedule_settings import restore_defaults
            restore_defaults(self.base)
            self.refresh();self.action_text.set('標準の2予約を保存しました。Windowsへの反映を開始します。');self.run_action('sync')
        except (OSError,ValueError):
            messagebox.showerror('標準予約を復元できません','予約設定の保存先と同時編集の有無を確認してください。',parent=self.dialog)

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
        warnings=self.refresh_reservations()
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
        self.run_rows={}
        for report in reports:
            if report['warning']:warnings.append(report['warning'])
            for row in report['rows']:
                identifier=self.run_tree.insert('','end',values=row[:4]+(row[6],)+row[4:6])
                self.run_rows[identifier]=row
        self.run_details.set('取得に失敗した行を選ぶと、理由と次に確認することを表示します。')
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
            if record is None:
                lines.append(label+'：記録なし。現在の予定は予約タブで確認してください。');continue
            line=label+'：予定 '+display_stamp(item['scheduled_for'][role])
            values=record['summary']['values']
            line+=' ／ 証券預り金 '+amount(values.get('broker_cash'))+'円・銀行等 '+amount(values.get('bank'))+'円'
            for name,title in (('growth_remaining','NISA成長枠'),('tsumitate_remaining','NISAつみたて枠')):
                nisa=values.get(name)
                if nisa is not None:line+=' ／ '+title+' '+str(nisa['year'])+'年 '+amount(nisa.get('remaining'))+'円'
            if record['summary']['displayed_at']:line+=' ／ 画面時計 '+record['summary']['displayed_at']+'（評価基準時刻とは未確認）'
            lines.append(line)
        self.details.set('\n'.join(lines))

    def select_run_row(self,_event=None):
        selected=self.run_tree.selection()
        row=self.run_rows.get(selected[0]) if selected else None
        if row is None:return
        self.run_details.set(row[0]+'・口座'+row[1]+'：'+(row[6] or '失敗理由の記録はありません。実取得日時と不足項目を確認してください。'))

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
        if action not in ('install','sync','status','test','uninstall'):return
        if self.app.busy:
            messagebox.showinfo('実行中','現在の処理が終わるまでお待ちください。',parent=self.dialog);return
        if os.name!='nt':
            messagebox.showinfo('Windowsで実行','SBI・Gmailを設定した同じWindowsユーザーで実行してください。この環境では登録・実取得を行いません。',parent=self.dialog);return
        logon='Password' if self.logon.get()=='サインアウト中も実行（パスワード）' else 'InteractiveToken'
        argv=[sys.executable,'-c',_CONSOLE_LAUNCHER,str(self.base/'windows_tasks.py'),'--action',action,
              '--logon-mode',logon,'--role','month-end']
        if action=='test':
            if getattr(self,'test_show_browser',None) is not None and self.test_show_browser.get():argv.append('--show-browser')
            if getattr(self,'test_include_pdfs',None) is not None and self.test_include_pdfs.get():argv.append('--include-pdfs')
        self._set_busy()
        try:
            self.process=subprocess.Popen(argv,cwd=str(self.base),creationflags=getattr(subprocess,'CREATE_NEW_CONSOLE',0))
        except OSError:
            self.process=None;self._restore()
            self.action_text.set('操作を起動できません。Windows・Python・アプリの配置先を確認してください。');return
        self.running_action=action
        name={'install':'タスク登録','sync':'Windows予約への反映','status':'登録状態の確認','test':'現在値のテスト取得','uninstall':'登録解除'}[action]
        self.action_text.set(name+'を別画面で実行しています。結果を確認し Enter を押すまで、このアプリの他の操作は停止します。')
        self.app.root.after(500,lambda:self.poll_action(name))

    def poll_action(self,name):
        if self.process is None:return
        code=self.process.poll()
        if code is None:
            self.app.root.after(500,lambda:self.poll_action(name));return
        self.process=None;self._restore()
        if code and getattr(self,'running_action',None) in ('sync','install'):
            self.action_text.set('予約設定は保存済みですが、Windowsへの反映を完了できませんでした（終了コード '+str(code)+'）。別画面の理由を確認し「Windows予約を反映」から再試行してください。')
        else:self.action_text.set(name+'が終了しました（終了コード '+str(code)+'）。保存済みレポートの取得日時・不足項目・理由も確認してください。')
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
