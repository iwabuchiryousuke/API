"""Local public-data editor. Unknown facts stay blank; no website credentials."""
from pathlib import Path
from datetime import date
import json,tkinter as tk
from tkinter import ttk,messagebox,simpledialog
from market_data import load_research,previous_close
from secure_settings import load,save
BASE=Path(__file__).resolve().parent

def run():
    root=tk.Tk();root.title('配当予定・月末と1日の終値');root.geometry('900x800')
    year=date.today().year;data=load_research(BASE/'data',year);data.setdefault('year',year);facts=data.setdefault('securities',{})
    body=ttk.Frame(root,padding=20);body.pack(fill='both',expand=True)
    ttk.Label(body,text='無料取得はYahoo Finance（yfinance）に統一。参考値とPDFの実入金を区別し、欠損は推測しません。').pack(anchor='w')
    selected=tk.StringVar();box=ttk.Combobox(body,textvariable=selected,values=list(facts),state='normal');box.pack(anchor='w',pady=8)
    fields={};labels={'name':'銘柄名','rights':'権利付き最終日（表示用）','payments':'支払日（表示用）','earnings':'本決算発表日（表示用）','dps':'今年権利分DPS（分割後換算）','close':'前月最終営業日終値（円）','close_date':'前月終値の日付 YYYY-MM-DD','month_start_close':'当月1日終値（円）','month_start_close_date':'当月1日の株価日 YYYY-MM-DD','source':'公式情報のURL・確認日','status':'確認メモ'}
    frame=ttk.Frame(body);frame.pack(fill='x')
    for r,(key,label) in enumerate(labels.items()):
        ttk.Label(frame,text=label).grid(row=r,column=0,sticky='w',pady=3);v=tk.StringVar();fields[key]=v;ttk.Entry(frame,textvariable=v,width=70).grid(row=r,column=1,pady=3)
    ttk.Label(body,text='予定明細JSON：rights＝権利最終日、payment＝支払日、dps＝その回の一株配当。翌年はpayment_year。').pack(anchor='w',pady=(10,0))
    plans=tk.Text(body,height=8);plans.pack(fill='x');complete=tk.BooleanVar();ttk.Checkbutton(body,text='今年支払の全予定を確認済み（確認できた場合だけ）',variable=complete).pack(anchor='w')
    def choose(*_):
        f=facts.get(selected.get(),{})
        for k,v in fields.items():v.set('' if f.get(k) is None else str(f.get(k,'')))
        plans.delete('1.0','end');plans.insert('1.0',json.dumps(f.get('events',[]),ensure_ascii=False,indent=2));complete.set(f.get('calendar_complete',False))
        ref=f.get('yahoo_reference')
        reference_label.configure(text=('Yahoo参考：権利落ち日 '+str(ref.get('ex_date') or '未取得')+' / 支払日 '+str(ref.get('payment_date') or '未取得')+' / 履歴 '+str(len(ref.get('historical_dividends',[])))+'件（年間予定ではありません）') if ref else '')
    reference_label=ttk.Label(body,text='',wraplength=820);reference_label.pack(anchor='w')
    box.bind('<<ComboboxSelected>>',choose)
    def persist():
        folder=BASE/'data'/'market';folder.mkdir(parents=True,exist_ok=True);target=folder/f'research_{year}.json'
        if target.exists():
            from datetime import datetime
            target.with_name(f'research_{year}_{datetime.now():%Y%m%d_%H%M%S_%f}.bak.json').write_bytes(target.read_bytes())
        tmp=target.with_suffix('.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8');tmp.replace(target)
    def commit():
        try:
            import re,math
            code=selected.get().strip().upper()
            if not re.fullmatch(r'[0-9A-Z]{4}',code):raise ValueError('コードは4文字です。')
            f={k:v.get().strip() for k,v in fields.items()}
            for k in ('dps','close','month_start_close'):
                f[k]=float(f[k].replace(',','')) if f[k] else None
                if f[k] is not None and (not math.isfinite(f[k]) or f[k]<0):raise ValueError('DPS・終値は0以上の有限数値です。')
            if f['close_date']:date.fromisoformat(f['close_date'])
            if f['month_start_close_date']:
                stamp=date.fromisoformat(f['month_start_close_date'])
                if stamp.day!=1:raise ValueError('当月1日終値の日付は実際の1日を指定してください。休場日は空欄にします。')
            if not f['source']:raise ValueError('出典を入力してください。')
            events=json.loads(plans.get('1.0','end'))
            if not isinstance(events,list):raise ValueError('予定明細はJSON配列です。')
            for e in events:
                if not isinstance(e,dict):raise ValueError('予定明細はオブジェクトです。')
                if e.get('rights'):date.fromisoformat(e['rights'])
                if e.get('payment'):date.fromisoformat(e['payment'])
                if e.get('dps') is not None and (not math.isfinite(float(e['dps'])) or float(e['dps'])<0):raise ValueError('一株配当を確認してください。')
            f.update(events=events,calendar_complete=complete.get());facts[code]=f;persist();box['values']=list(facts);messagebox.showinfo('保存','次のExcel出力に反映します。')
        except Exception as e:messagebox.showerror('入力確認',str(e))
    def update_yahoo(prices_only=False):
        try:
            from yahoo_market import fetch,merge_reference,month_closes
            from security_names import load as names_load
            import re,threading,queue,time
            names=names_load(BASE/'data')
            codes=sorted(c for c in set(facts)|set(names) if re.fullmatch(r'[0-9][0-9A-Z]{3}',c))
            if not codes:raise ValueError('銘柄コードを先に登録してください。')
            month=None
            if prices_only:
                month=simpledialog.askstring('基準月','例：2026-10 → 9月最終営業日と10月1日の終値。1日が休場日なら空欄。',initialvalue=date.today().strftime('%Y-%m'))
                if not month:return
                date.fromisoformat(month+'-01')
            for w in buttons.winfo_children():w.configure(state='disabled')
            results=queue.Queue()
            def worker():
                good={};failed=[]
                for c in codes:
                    try:good[c]=month_closes(c,month) if prices_only else fetch(c,year)
                    except Exception:failed.append(c)
                    time.sleep(.5)
                results.put((good,failed))
            def poll():
                try:good,failed=results.get_nowait()
                except queue.Empty:root.after(200,poll);return
                for w in buttons.winfo_children():w.configure(state='normal')
                for c,value in good.items():
                    old=facts.setdefault(c,{'name':names.get(c,'')})
                    if prices_only:old.update(value)
                    else:facts[c]=merge_reference(old,value,year)
                if good:persist()
                box['values']=list(facts);choose()
                messagebox.showinfo('更新結果',f'取得 {len(good)}銘柄 / 未取得 {len(failed)}銘柄'+('：'+', '.join(failed) if failed else '')+'\n未取得の既存値は維持します。配当の実入金はPDFから集計します。Yahooの権利落ち日を支払日として扱いません。')
            threading.Thread(target=worker,daemon=True).start();root.after(200,poll)
        except Exception as e:messagebox.showerror('更新停止',str(e))
    def prices():update_yahoo(True)
    def dividends():update_yahoo(False)
    buttons=ttk.Frame(body);buttons.pack(pady=12);ttk.Button(buttons,text='公開情報を保存',command=commit).pack(side='left',padx=8);ttk.Button(buttons,text='月末・1日の終値を更新',command=prices).pack(side='left',padx=8)
    ttk.Button(buttons,text='無料で配当参考情報を取得',command=dividends).pack(side='left',padx=8)
    if facts:box.current(0);choose()
    root.mainloop()
if __name__=='__main__':run()
