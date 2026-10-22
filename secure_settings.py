"""Generic local settings protected by the existing Windows DPAPI mechanism."""
import json,base64,re
from pathlib import Path
from login_credentials import _dpapi,CredentialError

def path(directory,name):
    if not re.fullmatch(r'[a-zA-Z0-9_-]+',name):raise CredentialError('設定名が不正です。')
    return Path(directory)/'private'/f'{name}.secure'
def save(directory,name,value):
    target=path(directory,name);target.parent.mkdir(parents=True,exist_ok=True)
    encrypted=_dpapi(json.dumps(value,ensure_ascii=False).encode(),protect=True)
    temporary=target.with_suffix('.tmp');temporary.write_bytes(base64.b64encode(encrypted));temporary.replace(target)
def load(directory,name):
    target=path(directory,name)
    if not target.exists():return None
    try:return json.loads(_dpapi(base64.b64decode(target.read_bytes(),validate=True),protect=False))
    except CredentialError:raise
    except Exception:raise CredentialError('暗号化設定を開けません。このWindowsユーザーで設定し直してください。') from None
