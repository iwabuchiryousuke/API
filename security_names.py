"""User display names, keyed by stable security code; never edit source names."""
import json,re,os
from pathlib import Path
from ledger import LedgerError

def load(directory):
    p=Path(directory)/'security_names.json'
    if not p.exists():
        defaults=Path(__file__).parent/'reference'/'security_names_default.json'
        return json.loads(defaults.read_text(encoding='utf-8')) if defaults.exists() else {}
    value=json.loads(p.read_text(encoding='utf-8'))
    if not isinstance(value,dict):raise LedgerError('銘柄辞書の形式を確認してください。')
    return value

def save(directory,code,name):
    code=str(code).strip().upper();name=str(name).strip()
    if not re.fullmatch(r'(?:[0-9A-Z]{4,6}|F-[0-9A-F]{10})',code):raise LedgerError('コードは4～6文字の数字・英字、または表示されているF-コードです。')
    if not name or len(name)>100:raise LedgerError('銘柄名は1～100文字です。')
    p=Path(directory)/'security_names.json';data=load(directory);data[code]=name
    tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(tmp,p)

def edit(parent,store):
    import tkinter as tk
    from tkinter import ttk,messagebox
    top=tk.Toplevel(parent);top.title('銘柄名・コードを設定');top.geometry('620x480')
    ttk.Label(top,text='表示する銘柄名を登録します。PDF原本と取引の識別は変更しません。',padding=12).pack()
    tree=ttk.Treeview(top,columns=('code','name'),show='headings');tree.heading('code',text='コード');tree.heading('name',text='表示名');tree.pack(fill='both',expand=True,padx=12)
    from report_legacy import reporting_code
    names={reporting_code(t):t['name'] for t in store.records()};names.update(load(store.directory))
    for k,v in sorted(names.items()):tree.insert('','end',values=(k,v))
    frame=ttk.Frame(top,padding=12);frame.pack(fill='x');code=tk.StringVar();name=tk.StringVar()
    ttk.Label(frame,text='コード').grid(row=0,column=0);ttk.Entry(frame,textvariable=code,width=18).grid(row=0,column=1)
    ttk.Label(frame,text='表示名').grid(row=1,column=0);ttk.Entry(frame,textvariable=name,width=45).grid(row=1,column=1)
    def selected(*_):
        if tree.selection():code.set(tree.item(tree.selection()[0],'values')[0]);name.set(tree.item(tree.selection()[0],'values')[1])
    tree.bind('<<TreeviewSelect>>',selected)
    def commit():
        try:save(store.directory,code.get(),name.get());messagebox.showinfo('保存','次の出力から反映します。',parent=top);top.destroy()
        except Exception as e:messagebox.showerror('入力確認',str(e),parent=top)
    ttk.Button(frame,text='保存',command=commit).grid(row=2,column=1,pady=10)
