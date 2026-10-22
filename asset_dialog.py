"""Read-only asset views: committed data, pending captures and dated evidence."""
import tkinter as tk
from tkinter import ttk
from datetime import datetime,timedelta,timezone
from asset_snapshots import combine_assets
from asset_history import preserve_snapshot

FIELDS=('domestic','foreign','cash','foreign_cash','growth_remaining','tsumitate_remaining')

def display_stamp(value):
    if not value:return '未取得'
    try:
        stamp=datetime.fromisoformat(value)
        if stamp.tzinfo is None:return '時刻未確認'
        return stamp.astimezone(timezone(timedelta(hours=9))).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError,TypeError):return '時刻未確認'

def amount(value):
    return '未取得' if value is None else f'{value:,.2f}'.rstrip('0').rstrip('.')

def current_rows(preview,accounts,today):
    rows=[]
    for account in accounts:
        key=account['id'];observations=preview['observations']
        summary=observations.get((key,'口座サマリー'),{})
        assets=observations.get((key,'My資産'),{})
        sv=summary.get('values',{});av=assets.get('values',{});values=combine_assets(av)
        cash=sv.get('cash_total') if sv.get('cash_total') is not None else values.get('cash')
        state='取得済・未反映' if key in preview['pending_accounts'] else '登録済' if summary or assets else '未取得'
        if key=='E':state='手動口座'
        stamps=[display_stamp(item.get('observed_at'))[:10] for item in (summary,assets) if item]
        if stamps and any(day!=today for day in stamps):state+='／以前の取得を含む'
        row=[account['label'],state,display_stamp(summary.get('observed_at')),display_stamp(assets.get('observed_at')),
             amount(values['domestic']),amount(values['foreign']),amount(cash),amount(values['foreign_cash']),amount(av.get('usd_cash'))]
        for field in ('growth_remaining','tsumitate_remaining'):
            item=sv.get(field,{})
            row.append(amount(item.get('remaining'))+(f"（{item['year']}年）" if 'year' in item else ''))
        row.extend((amount(av.get('domestic')),amount(av.get('foreign'))))
        rows.append(row)
    return rows

def boundary_rows(preview,accounts,dates):
    rows=[]
    for account in accounts:
        key=account['id']
        for day in dates:
            saved=preview['snapshots'].get((key,day))
            candidate=preview.get('boundary_candidates',{}).get(key,{}).get(day,{})
            note='当日の実測記録がありません。後日の現在値からは復元しません。'
            values={};state='未取得'
            if key=='E':state='手動口座';note='父の口座は管理Excelへ手入力してください。'
            elif saved is not None and preserve_snapshot(saved):
                source=str(saved.get('source') or '')
                values=saved;state='手動確認' if source=='手動確認' else '基準日を確認済み' if source.startswith('SBI') else '保存済み記録'
                note=saved.get('note','')
            elif candidate.get('available') and not candidate.get('conflicts'):
                values=candidate['values'];state='当日実測（参考）'
                note=(candidate.get('observed_from') or '')+' ～ '+(candidate.get('observed_to') or '')+' JST。月末終値・月初開始時点ではありません。'
            elif saved:
                values=saved;state='保存済み参考記録';note=saved.get('note','')
                if saved.get('source')=='SBI My資産の前月比':
                    state='旧版の前月比推定';note='基準時刻・指定日残高は未確認。'+note
            if candidate.get('conflicts') and state=='未取得':note='当日の観測値に競合があります。推定せず既存値を保持します。'
            rows.append([account['label'],day,state,*[amount(values.get(field)) for field in FIELDS],note])
    return rows

def history_rows(preview,accounts):
    labels={account['id']:account['label'] for account in accounts};rows=[]
    names={'domestic':'国内区分の表示分','foreign':'外国区分の表示分','cash':'円預り金','foreign_cash':'外貨現金の円評価',
           'bank':'連携銀行残高','fund':'投資信託','bond':'円建債券','usd_fund':'外貨建MMFの円評価',
           'usd_bond':'外貨建債券の円評価','usd_cash':'米ドル現金','cash_total':'円現金合計','broker_cash':'証券口座の現金',
           'growth_remaining':'NISA成長枠','tsumitate_remaining':'NISAつみたて枠'}
    for item in reversed(preview.get('history_observations',[])):
        payload=item['payload'];values=payload.get('values',{})
        shown=[]
        for key,value in values.items():
            if value is None or key not in names:continue
            if key in ('growth_remaining','tsumitate_remaining'):
                shown.append(names[key]+'='+amount(value.get('remaining'))+f"円（{value['year']}年）")
            else:shown.append(names[key]+'='+amount(value)+('ドル' if key=='usd_cash' else '円'))
        rows.append([labels.get(item['account'],item['account']),item['kind'],display_stamp(item['observed_at']),
                     payload.get('displayed_at') or '表示時計なし',
                     '、'.join(shown)])
    return rows

def _table(parent,headers,rows,widths=None):
    frame=ttk.Frame(parent,padding=8);frame.pack(fill='both',expand=True)
    columns=tuple(str(index) for index in range(len(headers)))
    tree=ttk.Treeview(frame,columns=columns,show='headings')
    for index,(column,header) in enumerate(zip(columns,headers)):
        tree.heading(column,text=header);tree.column(column,width=(widths or {}).get(index,145),anchor='w',stretch=False)
    for row in rows:tree.insert('','end',values=row)
    vertical=ttk.Scrollbar(frame,orient='vertical',command=tree.yview)
    horizontal=ttk.Scrollbar(frame,orient='horizontal',command=tree.xview)
    tree.configure(yscrollcommand=vertical.set,xscrollcommand=horizontal.set)
    tree.grid(row=0,column=0,sticky='nsew');vertical.grid(row=0,column=1,sticky='ns');horizontal.grid(row=1,column=0,sticky='ew')
    frame.rowconfigure(0,weight=1);frame.columnconfigure(0,weight=1)
    return tree

def show(app,base):
    from acquisition_targets import target_dates
    from asset_preview import preview_assets
    from sbi_views import _jst_now
    today=_jst_now().date().isoformat();dates=target_dates(app.month.get().strip())
    preview=preview_assets(base,app.store,today,target_dates=dates);accounts=app.store.accounts()
    dialog=tk.Toplevel(app.root);dialog.title('取得した資産・NISAと日付付き履歴');dialog.geometry('1180x600');dialog.minsize(850,420);dialog.transient(app.root)
    ttk.Label(dialog,text='取得済・未反映の証跡も表示します。台帳への登録は「取得済みPDFを全口座へ一括反映」で確認してください。',padding=12,wraplength=1100).pack(anchor='w')
    for warning in preview['warnings']:
        ttk.Label(dialog,text=warning,foreground='#955400',wraplength=1100,padding=(12,2)).pack(anchor='w')
    book=ttk.Notebook(dialog);book.pack(fill='both',expand=True,padx=10,pady=8)
    latest=ttk.Frame(book);dated=ttk.Frame(book);history=ttk.Frame(book)
    book.add(latest,text='最新の取得値');book.add(dated,text='前月末・当月1日');book.add(history,text='取得日時の履歴')
    ttk.Label(latest,text='国内株式等・外国資産の合計は投信や債券も確認できた場合だけ表示します。右端の「表示分」は読み取れた元項目です。サマリーとMy資産の取得日時は別々です。',padding=8,wraplength=1080).pack(anchor='w')
    _table(latest,['口座','登録状態','サマリー取得日時','My資産取得日時','国内株式等合計（円）','外国資産合計（円）','円現金合計','外貨現金（円評価）','米ドル現金（ドル）','NISA成長枠','NISAつみたて枠','国内区分・表示分（円）','外国区分・表示分（円）'],current_rows(preview,accounts,today),{1:205,2:180,3:180})
    ttk.Label(dated,text='基準日の直接確認と、その日に取得した参考記録を区別します。当日の記録がない過去日は補完しません。',padding=8,wraplength=1080).pack(anchor='w')
    _table(dated,['口座','基準日','確認区分','国内株式等（円）','外国株式（円）','円現金','外貨現金（円）','NISA成長枠','NISAつみたて枠','取得時刻・確認'],boundary_rows(preview,accounts,dates),{2:190,9:650})
    ttk.Label(history,text='自動取得を実行した日時を蓄積します。表示時計は評価基準時刻と確認できない場合があります。実行していない日の残高は記録できません。',padding=8,wraplength=1080).pack(anchor='w')
    _table(history,['口座','取得画面','取得日時（日本時間）','画面の表示時計','取得した表示値'],history_rows(preview,accounts),{1:150,2:200,3:200,4:900})
    ttk.Button(dialog,text='閉じる',command=dialog.destroy).pack(pady=(0,12))
