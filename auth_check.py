"""Read-only local Gmail diagnostic: never log message content or OTP values."""
from pathlib import Path
from datetime import datetime
from gmail_auth import ensure_service,message_reason,message_text,extract_code,extract_with_raw,SENDER,explain
from otp_parser import features
BASE=Path(__file__).resolve().parent

def run(progress=print):
    lines=['v3.5.9 Gmail接続・形式診断（SBIへのログイン・コード送信はしません）']
    def emit(text):lines.append(text);progress(text)
    for account in 'ABCD':
        try:
            api,recipient=ensure_service(BASE/'data',account,emit)
            response=api.users().messages().list(userId='me',q=f'from:{SENDER} newer_than:7d',maxResults=10,includeSpamTrash=False).execute()
            checked=0
            for item in response.get('messages',[]):
                m=api.users().messages().get(userId='me',id=item['id'],format='full').execute()
                reason=message_reason(m,recipient,0)
                if reason=='SUBJECT':continue
                checked+=1
                if reason!='VERIFIED':emit(f'口座{account} 候補{checked}：{reason}');continue
                f=features(message_text(m.get('payload',{})))
                value,method=extract_with_raw(api,item['id'],m,recipient,0,extract_code)
                emit(f'口座{account} 候補{checked}：本文あり={f["body_present"]}、ラベル数={f["labels"]}、候補数={f["candidates"]}、6文字解析={bool(value)}、方式={method}')
                del value
            if not checked:emit(f'口座{account}：直近7日の候補10件内に認証メールなし')
        except Exception as e:emit(f'口座{account}：{explain(e)}')
    folder=BASE/'data'/'diagnostics';folder.mkdir(parents=True,exist_ok=True)
    output=folder/('gmail_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'.txt')
    output.write_text('\n'.join(lines),encoding='utf-8')
    return output

if __name__=='__main__':
    try:print('診断保存先：'+str(run()))
    except Exception:print('診断ファイルを作成できませんでした。保存先を確認してください。')
    input('Enterで閉じます：')
