"""Explicit, evidence-referenced split entry when the notice PDF is unavailable."""
import json
from decimal import Decimal
from datetime import date,timedelta
from activity import key
from ledger import LedgerError,parse_date
from cost_basis import calculate

def register(store,activities,account,code,day,old,new,evidence):
    activities._valid_account(account);day=parse_date(day);code=str(code).strip().upper()
    try:old=Decimal(str(old));new=Decimal(str(new))
    except Exception:raise LedgerError('分割比率を数値で入力してください。')
    if not old.is_finite() or not new.is_finite() or old<=0 or new<=old:raise LedgerError('1対3など、株数が増える分割比率を入力してください。')
    if not evidence.strip():raise LedgerError('確認した通知の名前・公式資料URL等を入力してください。')
    for e in activities.events():
        if e['account']==account and e['type']=='株式分割' and e['code']==code and e['date']==day:raise LedgerError('同じ口座・銘柄・日の分割は登録済みです。')
    _,states=calculate(store,activities,(date.fromisoformat(day)-timedelta(days=1)).isoformat())
    held=[(k,s) for k,s in states.items() if k[:2]==(account,code) and s['quantity']>0]
    if not held or any(not s['valid'] for k,s in held):raise LedgerError('分割前の株数・取得履歴を確認できません。先に古いPDFを取り込んでください。')
    allocations={};before=Decimal(0);added=Decimal(0)
    for k,s in held:
        b=s['quantity'];after=b*new/old
        if after!=after.to_integral_value():raise LedgerError('端数株が発生する分割は未対応です。')
        allocations[k[2]]=[str(b),str(after-b),str(after)];before+=b;added+=after-b
    event=dict(type='株式分割',date=day,code=code,name=held[0][1]['name'],ratio_old=float(old),ratio_new=float(new),before=str(before),added=str(added),after=str(before+added),allocations=allocations,currency='JPY',manual=True,evidence=evidence)
    store.backup()
    with store.db:store.db.execute('INSERT INTO activities(account,kind,event_key,event_date,payload,source) VALUES (?,?,?,?,?,?)',(account,event['type'],key(event),day,json.dumps(event,ensure_ascii=False),'手動確認：'+evidence))
    return event

def edit(app):
    from tkinter import simpledialog,messagebox
    try:
        account=app.selected()['id'];values=[]
        for prompt in ('銘柄コード','分割の効力発生日 YYYY-MM-DD','分割前の比率（1対3なら1）','分割後の比率（1対3なら3）','確認した通知名・公式資料URL'):
            value=simpledialog.askstring('株式分割を登録',prompt)
            if value is None:return
            values.append(value)
        if not messagebox.askyesno('登録内容の確認',f'口座{account}／コード{values[0]}／{values[1]}／{values[2]}対{values[3]}\n原本の効力発生日・比率を確認して登録しますか？'):return
        e=register(app.store,app.activities,account,*values);app.refresh();messagebox.showinfo('登録しました',f"増加株数 {e['added']}。取得総額を増やさず、出力では無色・0円のB行になります。")
    except Exception as e:messagebox.showerror('登録できません',str(e))
