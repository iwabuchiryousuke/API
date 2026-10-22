"""Read-only Gmail OTP integration. No message contents/codes in files or logs.
Initial OAuth consent is interactive. Read messages only after a new SBI login.
"""
import base64,html,re,time,unicodedata,copy
from email.header import decode_header,make_header
from urllib.parse import urlparse
from email.utils import getaddresses,parseaddr
from pathlib import Path
from secure_settings import load,save
from login_credentials import CredentialError
SCOPES=['https://www.googleapis.com/auth/gmail.readonly']
SENDER='info@sbisec.co.jp'

class GmailError(CredentialError):
    def __init__(self,code,message):self.code=code;super().__init__(f'[{code}] {message}')

def explain(error):
    """Only fixed diagnostic strings, never server responses, tokens or URLs."""
    if isinstance(error,GmailError):return error
    if isinstance(error,CredentialError):return GmailError('LOCAL_SETTINGS',str(error))
    code=str(getattr(error,'error',''))
    if isinstance(error,ImportError):return GmailError('DEPENDENCY','setup_all_windows.batを実行してください。')
    if code in ('access_denied','org_internal','admin_policy_enforced'):
        return GmailError('ACCESS_DENIED','Google側で許可されませんでした。テスト中のアプリはGoogle Cloudの「対象」でこの口座のGmailをテストユーザーへ追加してください。管理者による制限も確認してください。')
    if code in ('invalid_client','unauthorized_client','redirect_uri_mismatch'):
        return GmailError('CLIENT_CONFIG','デスクトップアプリ用OAuthクライアントJSONを確認してください。')
    if isinstance(error,(TimeoutError,AttributeError)):
        return GmailError('CONSENT_TIMEOUT','Googleの許可が完了しませんでした。ブラウザのエラー表示とテストユーザー登録を確認し、収集をもう一度押してください。')
    status=getattr(getattr(error,'resp',None),'status',None)
    if status==403:return GmailError('API_FORBIDDEN','Gmail APIを有効化し、このアカウントで読み取りを許可してください。Google Cloudのテストユーザー登録も確認してください。')
    if status==429:return GmailError('API_LIMIT','Gmail APIの利用上限です。時間をおいて再実行してください。')
    return GmailError('CONNECTION','Googleへ接続できません。ネットワーク、Google側の表示、デスクトップOAuth設定を確認してください。')

def validate_client(config):
    if not isinstance(config,dict):raise GmailError('CLIENT_CONFIG','Googleのデスクトップアプリ用OAuth JSONが必要です。')
    c=config.get('installed',{})
    if not isinstance(c,dict) or not isinstance(c.get('client_id'),str) or not c['client_id'].endswith('.apps.googleusercontent.com') or not isinstance(c.get('client_secret'),str) or not c['client_secret'] or c.get('auth_uri')!='https://accounts.google.com/o/oauth2/auth' or c.get('token_uri')!='https://oauth2.googleapis.com/token':
        raise GmailError('CLIENT_CONFIG','Googleのデスクトップアプリ用OAuth JSONを使用してください。')
    return config

def check_client(setting,active):
    if not active:return
    expected=validate_client(active)['installed'];actual=setting.get('credentials',{})
    if actual.get('client_id')!=expected['client_id']:
        raise GmailError('CLIENT_CHANGED','添付されたOAuth設定に切り替わったため、この口座で再許可が必要です。')
    # Client-secret rotation does not change the grant's client identity.
    actual['client_secret']=expected['client_secret']
    actual['token_uri']=expected['token_uri']

def client_config(directory,client_path=None):
    import json
    directory=Path(directory)
    if client_path:
        try:config=json.loads(Path(client_path).read_text(encoding='utf-8-sig'))
        except Exception:raise GmailError('CLIENT_JSON','OAuth JSONを読み取れません。') from None
    else:
        config=load(directory,'gmail_client')
        if not config:
            for a in 'ABCD':
                setting=load(directory,'gmail_'+a) or {};c=setting.get('credentials',{})
                if c.get('client_id') and c.get('client_secret'):
                    config={'installed':{'client_id':c['client_id'],'client_secret':c['client_secret'],'auth_uri':'https://accounts.google.com/o/oauth2/auth','token_uri':'https://oauth2.googleapis.com/token','redirect_uris':['http://localhost']}};break
        if not config:
            files=list((directory/'private').glob('*.json'))+list(directory.parent.glob('client_secret*.json'))
            configs=[]
            for p in files:
                try:
                    value=json.loads(p.read_text(encoding='utf-8-sig'))
                    if 'installed' in value:configs.append(value)
                except (ValueError,OSError):continue
            if len(configs)==1:config=configs[0]
            elif len(configs)>1:raise GmailError('CLIENT_AMBIGUOUS','OAuth JSONが複数あります。使用するデスクトップJSONだけをdata/privateに置いてください。')
    if not config:raise GmailError('CLIENT_MISSING','既存A口座の連携設定か、デスクトップOAuth JSONが必要です。旧dataをコピーするか、JSONをdata/privateへ置いて再実行してください。')
    validate_client(config)
    # Resolving a candidate is read-only. Persist only after consent, mailbox
    # identity and scopes have all passed; failed consent must preserve grants.
    return config

def recipient_for(directory,account,setting=None):
    """Use the saved account binding; defaults initialize a missing binding only."""
    from family_profile import MAILBOXES
    if account not in MAILBOXES:
        raise GmailError('ACCOUNT_CONFIG','Gmail自動取得はA～D口座です。')
    if setting is None:setting=load(directory,f'gmail_{account}')
    if setting is not None and not isinstance(setting,dict):
        raise GmailError('LOCAL_SETTINGS','保存済みGmail設定の形式を確認してください。設定は変更していません。')
    recipient=(setting or {}).get('recipient',MAILBOXES[account])
    if not isinstance(recipient,str) or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',recipient.strip()):
        raise GmailError('MAILBOX_CONFIG','保存済みのGmail受信先を確認してください。設定は変更していません。')
    return recipient.strip().lower()

def save_connection(directory,account,config,setting):
    """Rollback both encrypted files if a local persistence operation fails."""
    from secure_settings import path
    names=[path(directory,'gmail_client'),path(directory,f'gmail_{account}')]
    names+=[p.with_suffix('.tmp') for p in names]
    before={p:p.read_bytes() if p.exists() else None for p in names}
    try:
        save(directory,'gmail_client',config)
        save(directory,f'gmail_{account}',setting)
    except Exception:
        for p,raw in before.items():
            if raw is None:p.unlink(missing_ok=True)
            else:p.write_bytes(raw)
        raise

def verify_mailbox(api,expected):
    try:actual=api.users().getProfile(userId='me').execute().get('emailAddress','').lower()
    except Exception as e:raise explain(e) from None
    if actual!=expected.lower():raise GmailError('MAILBOX_MISMATCH','選択されたGoogleアカウントが、この口座の指定Gmailと異なります。指定されたアカウントを選び直してください。')

def connect(directory,account,client_path=None,recipient=None):
    import json
    try:
        recipient=recipient_for(directory,account,{'recipient':recipient}) if recipient is not None else recipient_for(directory,account)
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        config=client_config(directory,client_path)
        flow=InstalledAppFlow.from_client_config(config,SCOPES,autogenerate_code_verifier=True)
        creds=flow.run_local_server(port=0,open_browser=True,timeout_seconds=180,login_hint=recipient,prompt='select_account consent',access_type='offline',authorization_prompt_message='',success_message='認可が完了しました。アプリへ戻ってください。')
        if not creds.refresh_token:raise GmailError('OFFLINE_PERMISSION','継続利用の許可を取得できませんでした。Googleの許可画面でGmail読み取りを許可してください。')
        api=build('gmail','v1',credentials=creds,cache_discovery=False)
        verify_mailbox(api,recipient)
        if not creds.has_scopes(SCOPES):raise GmailError('SCOPE_MISSING','Gmailの読み取りが許可されていません。収集を再実行して読み取りを許可してください。')
        save_connection(directory,account,config,{'credentials':json.loads(creds.to_json()),'recipient':recipient})
        return api,recipient
    except Exception as e:raise explain(e) from None

def service(directory,account):
    import json
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        setting=copy.deepcopy(load(directory,f'gmail_{account}'))
        if not setting:raise GmailError('NEEDS_CONSENT','初回のGoogle利用許可が必要です。')
        recipient=recipient_for(directory,account,setting)
        check_client(setting,load(directory,'gmail_client'))
        creds=Credentials.from_authorized_user_info(setting['credentials'],SCOPES)
        if not creds.has_scopes(SCOPES):raise GmailError('SCOPE_MISSING','Gmail読み取りの再許可が必要です。')
        if not creds.valid:
            if not creds.refresh_token:raise GmailError('NEEDS_CONSENT','Googleの利用許可を設定し直します。')
            try:creds.refresh(Request())
            except Exception as e:
                from google.auth.exceptions import RefreshError
                if isinstance(e,RefreshError):raise GmailError('NEEDS_CONSENT','Googleの利用許可が失効しています。再認可が必要です。') from None
                raise
            setting['credentials']=json.loads(creds.to_json())
        api=build('gmail','v1',credentials=creds,cache_discovery=False)
        verify_mailbox(api,recipient)
        save(directory,f'gmail_{account}',setting)
        return api,recipient
    except Exception as e:raise explain(e) from None

def ensure_service(directory,account,progress=lambda message:None):
    try:
        session=service(directory,account)
        progress(f'口座{account}：保存済みGmail認可を再利用しました。')
        return session
    except GmailError as e:
        # A different mailbox/client requires deliberate settings review; never
        # replace a saved binding while trying to repair an authentication error.
        if e.code not in ('NEEDS_CONSENT','SCOPE_MISSING'):raise
        reason=e.code
    recipient=recipient_for(directory,account)
    progress(f'口座{account}：再許可の理由 [{reason}]')
    progress(f'口座{account}：Googleの許可画面で、この口座に設定済みのGmailを選び、読み取りを許可してください。')
    return connect(directory,account,recipient=recipient)

def message_text(payload):
    from otp_parser import visible_html,normalize
    from email.message import Message
    text=[]
    if payload.get('mimeType') in ('text/plain','text/html') and payload.get('body',{}).get('data'):
        raw=base64.urlsafe_b64decode(payload['body']['data']+'===')
        mime=Message();mime['Content-Type']=next((h['value'] for h in payload.get('headers',[]) if h['name'].lower()=='content-type'),'text/plain; charset=utf-8')
        try:body=raw.decode(mime.get_content_charset() or 'utf-8',errors='strict')
        except (LookupError,UnicodeError):body=raw.decode('utf-8',errors='replace')
        text.append(visible_html(body) if payload.get('mimeType')=='text/html' else body)
    for part in payload.get('parts',[]):
        if part.get('filename'):continue
        text.append(message_text(part))
    return '\n'.join(text)


def message_reason(message,recipient,started):
    """Fixed reason codes only; no email content in diagnostics."""
    try:
        if int(message.get('internalDate',0))/1000 < started:return 'OLD'
    except (TypeError,ValueError):return 'DATE'
    headers={h['name'].lower():h['value'] for h in message.get('payload',{}).get('headers',[])}
    if parseaddr(headers.get('from',''))[1].lower()!=SENDER:return 'SENDER'
    addresses={a.lower() for _,a in getaddresses([headers[k] for k in ('to','delivered-to','x-original-to') if headers.get(k)])}
    if recipient.lower() not in addresses:return 'RECIPIENT'
    auths=[h['value'].lower() for h in message.get('payload',{}).get('headers',[]) if h['name'].lower()=='authentication-results']
    good=[a for a in auths if re.match(r'\s*mx\.google\.com\s*;',a) and re.search(r'dkim=pass[^;]*header\.(?:i|d)=(?:@)?(?:[\w-]+\.)*sbisec\.co\.jp(?:[\s;]|$)',a)]
    if not good:return 'AUTHENTICITY'
    subject=str(make_header(decode_header(headers.get('subject',''))))
    if not re.search(r'(ログイン|認証)',subject):return 'SUBJECT'
    return 'VERIFIED'

def verified_message(message,recipient,started):
    if message_reason(message,recipient,started)!='VERIFIED':return None
    headers={h['name'].lower():h['value'] for h in message.get('payload',{}).get('headers',[])}
    return message_text(message.get('payload',{})),str(make_header(decode_header(headers.get('subject',''))))


def extract_code(message,recipient,started,challenge=None,length=None):
    from otp_parser import choose
    verified=verified_message(message,recipient,started)
    if not verified:return None
    body,_=verified
    if challenge and challenge not in body:return None
    return choose(body,length)


def extract_with_raw(api,item_id,msg,recipient,started,extractor):
    """Retry unreadable FULL using original MIME, with all checks retained."""
    value=extractor(msg,recipient,started)
    if value:return value,'FULL'
    from otp_parser import candidates
    if len(candidates(message_text(msg.get('payload',{}))))>1:return None,'AMBIGUOUS'
    from otp_parser import raw_payload
    result=api.users().messages().get(userId='me',id=item_id,format='raw').execute()
    if not isinstance(result.get('raw'),str):return None,'RAW_EMPTY'
    try:payload=raw_payload(result['raw'])
    except (ValueError,TypeError):return None,'RAW_INVALID'
    alternate={'internalDate':msg.get('internalDate'),'payload':payload}
    # The RAW and FULL authentication headers must both pass, independently.
    if message_reason(alternate,recipient,started)!='VERIFIED':return None,'RAW_UNVERIFIED'
    first=extractor(alternate,recipient,started)
    return first,('RAW' if first else 'FORMAT')


def extract_auth_link(message,recipient,started):
    verified=verified_message(message,recipient,started)
    if not verified:return None
    body,subject=verified
    if '認証コード入力画面' not in subject or 'デバイス認証' not in subject:return None
    # Keep href values from HTML mail as well as plain text.
    def raw_parts(payload):
        result=[]
        data=payload.get('body',{}).get('data')
        if data:result.append(base64.urlsafe_b64decode(data+'===').decode('utf-8',errors='replace'))
        for part in payload.get('parts',[]):result+=raw_parts(part)
        return result
    urls=set()
    for raw in raw_parts(message.get('payload',{})):
        for url in re.findall(r'https://[^\s<>"\']+',html.unescape(raw)):
            url=url.rstrip('。)）')
            parsed=urlparse(url)
            if parsed.hostname=='m.sbisec.co.jp' and parsed.port in (None,443) and not parsed.username and parsed.path not in ('','/'):
                urls.add(url)
    return urls.pop() if len(urls)==1 else None

def recent_ids(api,started):
    response=api.users().messages().list(userId='me',q=f'from:{SENDER} after:{int(started)-600}',maxResults=100,includeSpamTrash=False).execute()
    if response.get('nextPageToken'):raise CredentialError('認証メール候補が多すぎます。手動確認してください。')
    return {m['id'] for m in response.get('messages',[])}

def wait_message(api,recipient,started,extractor,seen=None,timeout=180,progress=lambda message:None,cancelled=lambda:False):
    from collections import Counter
    seen=set(seen or ());end=time.monotonic()+timeout;checked={};last_report=None
    labels={'OLD':'開始前のメール','DATE':'日時不明','SENDER':'送信者不一致','RECIPIENT':'宛先不一致','AUTHENTICITY':'送信元認証を確認できない','SUBJECT':'件名が対象外','FORMAT':'コード・リンク形式が未対応'}
    while time.monotonic()<end:
        if cancelled():raise CredentialError('中止しました。')
        response=api.users().messages().list(userId='me',q=f'from:{SENDER} after:{int(started)-1}',maxResults=100,includeSpamTrash=False).execute()
        if response.get('nextPageToken'):raise CredentialError('認証メール候補が多すぎます。手動で確認してください。')
        candidates=[]
        for item in response.get('messages',[]):
            if item['id'] in seen:continue
            if item['id'] not in checked:
                msg=api.users().messages().get(userId='me',id=item['id'],format='full').execute()
                reason=message_reason(msg,recipient,started)
                value=None
                if reason=='VERIFIED':
                    value,method=extract_with_raw(api,item['id'],msg,recipient,started,extractor)
                    if value:progress('認証コード・リンクを確認（'+method+'）')
                    else:
                        from otp_parser import features
                        f=features(message_text(msg.get('payload',{})))
                        progress(f'本文診断：本文あり={f["body_present"]}、ラベル数={f["labels"]}、候補数={f["candidates"]}、読取方式={method}')
                checked[item['id']]=(reason if reason!='VERIFIED' else ('MATCH' if value else 'FORMAT'),value)
            reason,value=checked[item['id']]
            if value:candidates.append((item['id'],value))
        if len(candidates)>1:raise CredentialError('新しい認証メールが複数あります。自動選択せず停止しました。')
        if len(candidates)==1:return candidates[0]
        counts=Counter(reason for reason,value in checked.values())
        report='／'.join(f'{labels.get(k,k)} {v}件' for k,v in sorted(counts.items())) or '開始後のSBIメール未検出（受信箱・SBI登録先を確認）'
        if report!=last_report:progress(report);last_report=report
        time.sleep(3)
    raise CredentialError(f'{timeout}秒以内に認証メールを確認できませんでした。診断：{last_report or "候補なし"}。迷惑メールは自動使用しません。')

def wait_code(api,recipient,started,seen=None,timeout=180,challenge=None,progress=lambda message:None,cancelled=lambda:False,length=None):
    return wait_message(api,recipient,started,lambda m,r,s:extract_code(m,r,s,challenge,length),seen,timeout,progress,cancelled)
