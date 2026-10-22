"""One-time encrypted personal provisioning, then normal Windows DPAPI storage."""
import hashlib,json
from pathlib import Path
from login_credentials import CredentialError,save_credentials,has_credentials
from secure_settings import load,save

def install(base,key):
    from cryptography.fernet import Fernet,InvalidToken
    payload=(Path(base)/'personal_setup.enc').read_bytes()
    try:config=json.loads(Fernet(key.strip().encode()).decrypt(payload))
    except (ValueError,InvalidToken,UnicodeError):raise CredentialError('初回設定キーが一致しません。設定は変更していません。') from None
    if not isinstance(config,dict):raise CredentialError('初回設定の内容を確認できません。')
    if config.get('version')==2:
        oauth=config.get('oauth');logins=config.get('logins',{})
        from gmail_auth import validate_client
        validate_client(oauth)
    else:oauth=None;logins=config
    if not isinstance(logins,dict) or set(logins)!=set('ABCD') or any(not isinstance(v,dict) or set(v)!=set(('username','password')) or not all(isinstance(x,str) and x for x in v.values()) for v in logins.values()):raise CredentialError('初回設定の内容を確認できません。')
    directory=Path(base)/'data'
    # An app update may contain older provisioning data. Keep the user's active
    # OAuth client just as we already keep saved SBI passwords.
    active_oauth=load(directory,'gmail_client')
    if active_oauth is not None:
        from gmail_auth import validate_client
        validate_client(active_oauth)
    # Restore all existing private settings if a DPAPI write fails mid-install.
    names=[directory/'private'/f'login_{a}.dat' for a in 'ABCD']+[directory/'private'/f'{n}.secure' for n in ('gmail_client','personal_setup','provision_key')]
    names+=[p.with_suffix('.tmp') for p in names]
    before={p:p.read_bytes() if p.exists() else None for p in names}
    try:
        for a,v in logins.items():
            if not has_credentials(directory,a):save_credentials(directory,a,v['username'],v['password'])
        if oauth and active_oauth is None:save(directory,'gmail_client',oauth)
        save(directory,'provision_key',{'key':key.strip()})
        save(directory,'personal_setup',{'digest':hashlib.sha256(payload).hexdigest(),'oauth_installed':bool(oauth)})
    except Exception:
        for p,data in before.items():
            if data is None:p.unlink(missing_ok=True)
            else:p.write_bytes(data)
        raise

def ready(base):
    base=Path(base);bundle=base/'personal_setup.enc'
    if not bundle.exists():return True
    marker=load(base/'data','personal_setup') or {}
    return marker.get('digest')==hashlib.sha256(bundle.read_bytes()).hexdigest() and bool(load(base/'data','gmail_client')) and all(has_credentials(base/'data',a) for a in 'ABCD')

def ensure_personal(base,parent):
    from tkinter import filedialog,messagebox
    base=Path(base);encrypted=base/'personal_setup.enc'
    if not encrypted.exists():return
    try:
        if ready(base):return
        previous=load(base/'data','provision_key')
        if previous and previous.get('key'):
            install(base,previous['key']);return
        messagebox.showinfo('初回だけの設定','同時にダウンロードした personal_setup.key を選択してください。\n四口座の入力済み情報と添付済みGmail API設定を暗号化して登録します。\n次回から選択は不要です。',parent=parent)
        key_path=filedialog.askopenfilename(parent=parent,title='初回設定キーを選択',filetypes=[('初回設定キー','*.key')])
        if not key_path:return
        install(base,Path(key_path).read_text(encoding='utf-8').strip())
        messagebox.showinfo('初回設定完了','ログイン情報とGmail API設定を登録しました。初回設定キーはアプリ外で保管してください。',parent=parent)
    except ImportError:messagebox.showerror('準備が必要です','setup_all_windows.batを一度実行してください。',parent=parent)
    except (CredentialError,OSError) as e:messagebox.showerror('初回設定',str(e) if isinstance(e,CredentialError) else '初回設定ファイルを読めません。',parent=parent)
