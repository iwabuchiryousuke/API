"""User-defined dividend summary order, separate from financial records."""
import json
import os
from pathlib import Path
import stat
import tempfile
import unicodedata

from ledger import LedgerError


FILENAME='dividend_order.json'
MAX_CODES=4096
MAX_BYTES=512*1024


def _codes(values):
    if not isinstance(values,(list,tuple)) or len(values)>MAX_CODES:
        raise LedgerError('配当シートの銘柄順の形式を確認してください。')
    result=[]
    for value in values:
        if not isinstance(value,str):raise LedgerError('配当シートの銘柄コードを確認してください。')
        code=unicodedata.normalize('NFKC',value).strip().upper()
        if not code or len(code)>100 or any(ord(character)<32 for character in code):
            raise LedgerError('配当シートの銘柄コードを確認してください。')
        if code in result:raise LedgerError('配当シートの銘柄順に同じコードが重複しています。')
        result.append(code)
    return result


def _ordinary(path):
    info=path.lstat()
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_size>MAX_BYTES:
        raise LedgerError('配当シートの銘柄順ファイルを確認してください。')


def _unique(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('Duplicate preference key')
        result[key]=value
    return result


def load(directory):
    """Read saved preference only; never create a file during Excel export."""
    path=Path(directory)/FILENAME
    if not path.exists() and not path.is_symlink():return []
    _ordinary(path)
    try:
        value=json.loads(path.read_text(encoding='utf-8'),object_pairs_hook=_unique)
        if (not isinstance(value,dict) or isinstance(value.get('schema'),bool)
                or value.get('schema')!=1):raise ValueError('Preference schema')
        return _codes(value.get('codes'))
    except (UnicodeError,ValueError,TypeError) as error:
        raise LedgerError('配当シートの銘柄順ファイルを確認してください。元のファイルは変更していません。') from error


def save(directory,codes):
    """Atomically save an order without touching ledger or name settings."""
    selected=_codes(codes)
    directory=Path(directory)
    if directory.is_symlink():raise LedgerError('銘柄順の保存先を確認してください。')
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/FILENAME
    if path.exists() or path.is_symlink():_ordinary(path)
    descriptor,temporary=tempfile.mkstemp(prefix='.dividend_order_',suffix='.tmp',dir=directory)
    temporary=Path(temporary)
    try:
        with os.fdopen(descriptor,'w',encoding='utf-8') as stream:
            json.dump({'schema':1,'codes':selected},stream,ensure_ascii=False,indent=2)
            stream.write('\n')
            stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)
    return selected


def ordered_codes(directory,codes):
    """Keep configured present codes first; append future codes predictably."""
    present=_codes(list(codes))
    known=set(present)
    configured=[code for code in load(directory) if code in known]
    return configured+sorted(known-set(configured))


def move_codes(codes,selected,offset):
    """Return the row order after a single selected row moves up or down."""
    result=_codes(list(codes))
    selected=unicodedata.normalize('NFKC',str(selected)).strip().upper()
    if selected not in result or offset not in (-1,1):return result
    index=result.index(selected);destination=index+offset
    if 0<=destination<len(result):result[index],result[destination]=result[destination],result[index]
    return result


def security_choices(store,activities=None):
    from report_legacy import reporting_code
    from security_names import load as load_names
    def canonical(value):return _codes([str(value)])[0]
    names={canonical(reporting_code(row)):row.get('name','') for row in store.records()}
    if activities is not None:
        for row in activities.events():
            if row.get('type')=='配当':
                code=canonical(row.get('code') or '未分類')
                names.setdefault(code,row.get('name') or 'コード未登録の配当')
    names.update({canonical(identifier):name for identifier,name in load_names(store.directory).items()})
    return names


def edit(parent,store,activities=None):
    """One visible setting for display names and the summary's top-to-bottom order."""
    import tkinter as tk
    from tkinter import ttk,messagebox
    from security_names import save as save_name
    names=security_choices(store,activities)
    order=ordered_codes(store.directory,names)
    top=tk.Toplevel(parent);top.title('銘柄設定');top.geometry('700x560');top.minsize(600,480)
    top.transient(parent)
    ttk.Label(top,text='配当シートの銘柄を、表示したい順に並べます。',padding=(14,12)).pack(anchor='w')
    body=ttk.Frame(top,padding=(14,0));body.pack(fill='both',expand=True)
    tree=ttk.Treeview(body,columns=('code','name'),show='headings',selectmode='browse')
    tree.heading('code',text='コード');tree.heading('name',text='表示名')
    tree.column('code',width=110,stretch=False);tree.column('name',width=450)
    tree.grid(row=0,column=0,sticky='nsew')
    scroll=ttk.Scrollbar(body,orient='vertical',command=tree.yview)
    scroll.grid(row=0,column=1,sticky='ns');tree.configure(yscrollcommand=scroll.set)
    body.rowconfigure(0,weight=1);body.columnconfigure(0,weight=1)
    for identifier in order:tree.insert('','end',iid=identifier,values=(identifier,names[identifier]))
    actions=ttk.Frame(top,padding=(14,8));actions.pack(fill='x')
    def move(offset):
        selection=tree.selection()
        if not selection:return
        selected=selection[0]
        changed=move_codes(list(tree.get_children()),selected,offset)
        for index,identifier in enumerate(changed):tree.move(identifier,'',index)
        tree.see(selected)
    ttk.Button(actions,text='上へ',command=lambda:move(-1)).pack(side='left',padx=(0,6))
    ttk.Button(actions,text='下へ',command=lambda:move(1)).pack(side='left',padx=(0,14))
    def save_order():
        try:
            save(store.directory,list(tree.get_children()))
            messagebox.showinfo('銘柄順を保存しました','次のExcel出力から、この順で配当の銘柄まとめを表示します。',parent=top)
        except (LedgerError,OSError,ValueError) as error:messagebox.showerror('保存できません',str(error),parent=top)
    ttk.Button(actions,text='この順番を保存',command=save_order).pack(side='left')
    ttk.Label(actions,text='新しい銘柄は末尾に追加します。').pack(side='left',padx=(14,0))
    form=ttk.Frame(top,padding=(14,8));form.pack(fill='x')
    code=tk.StringVar();name=tk.StringVar()
    ttk.Label(form,text='コード').grid(row=0,column=0,sticky='w',padx=(0,10),pady=4)
    ttk.Entry(form,textvariable=code,width=18).grid(row=0,column=1,sticky='w',pady=4)
    ttk.Label(form,text='表示名').grid(row=1,column=0,sticky='w',padx=(0,10),pady=4)
    ttk.Entry(form,textvariable=name).grid(row=1,column=1,sticky='ew',pady=4)
    form.columnconfigure(1,weight=1)
    def selected(*_):
        selection=tree.selection()
        if selection:
            values=tree.item(selection[0],'values');code.set(values[0]);name.set(values[1])
    tree.bind('<<TreeviewSelect>>',selected)
    def save_display_name():
        try:
            save_name(store.directory,code.get(),name.get())
            identifier=unicodedata.normalize('NFKC',code.get()).strip().upper()
            rendered=name.get().strip()
            if tree.exists(identifier):tree.item(identifier,values=(identifier,rendered))
            else:tree.insert('','end',iid=identifier,values=(identifier,rendered))
            tree.selection_set(identifier);tree.see(identifier)
            messagebox.showinfo('表示名を保存しました','次の表示・出力から反映します。銘柄順は「この順番を保存」で保存できます。',parent=top)
        except (LedgerError,OSError,ValueError) as error:messagebox.showerror('入力の確認',str(error),parent=top)
    ttk.Button(form,text='表示名を保存',command=save_display_name).grid(row=2,column=1,sticky='e',pady=8)
    ttk.Button(top,text='閉じる',command=top.destroy).pack(pady=(0,12))
    return top
