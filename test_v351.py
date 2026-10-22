"""Offline migration tests: fake DPAPI persistence, never real credentials."""
import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from cryptography.fernet import Fernet
from initial_setup import install,ready,ensure_personal
from gmail_auth import validate_client,check_client,ensure_service,GmailError
from login_credentials import CredentialError

def oauth(client='test'):
    return {'installed':{'client_id':client+'.apps.googleusercontent.com','client_secret':'dummy','auth_uri':'https://accounts.google.com/o/oauth2/auth','token_uri':'https://oauth2.googleapis.com/token'}}

class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name);self.key=Fernet.generate_key().decode()
        self.bundle={'version':2,'logins':{a:{'username':'user'+a,'password':'dummy'} for a in 'ABCD'},'oauth':oauth()};self.write_bundle()
        def save(d,n,v):
            p=Path(d)/'private';p.mkdir(parents=True,exist_ok=True);(p/(n+'.secure')).write_text(json.dumps(v))
        def load(d,n):
            p=Path(d)/'private'/(n+'.secure');return json.loads(p.read_text()) if p.exists() else None
        def login(d,a,u,pw):
            p=Path(d)/'private';p.mkdir(parents=True,exist_ok=True);(p/f'login_{a}.dat').write_text(u)
        self.patches=[patch('initial_setup.save',side_effect=save),patch('initial_setup.load',side_effect=load),patch('initial_setup.save_credentials',side_effect=login),patch('initial_setup.has_credentials',side_effect=lambda d,a:(Path(d)/'private'/f'login_{a}.dat').exists())]
        for p in self.patches:p.start()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def write_bundle(self):
        (self.base/'personal_setup.enc').write_bytes(Fernet(self.key.encode()).encrypt(json.dumps(self.bundle).encode()))
    def test_install_ready_and_preserves_updated_passwords(self):
        install(self.base,self.key);self.assertTrue(ready(self.base))
        old=self.base/'data/private/login_A.dat';old.write_text('updated-local')
        install(self.base,self.key);self.assertEqual(old.read_text(),'updated-local')
        self.assertTrue((self.base/'data/private/gmail_client.secure').exists())
    def test_partial_write_restores_every_file(self):
        install(self.base,self.key);private=self.base/'data/private';before={p.name:p.read_bytes() for p in private.iterdir()}
        from initial_setup import save
        original=save.side_effect
        def failing(d,n,v):
            original(d,n,v)
            if n=='provision_key':raise OSError('simulated')
        self.bundle['oauth']=oauth('changed');self.write_bundle()
        with patch('initial_setup.save',side_effect=failing):
            with self.assertRaises(OSError):install(self.base,self.key)
        self.assertEqual(before,{p.name:p.read_bytes() for p in private.iterdir()});self.assertFalse(ready(self.base))
    def test_wrong_key_changes_nothing(self):
        install(self.base,self.key)
        with patch('initial_setup.save') as save:
            with self.assertRaises(CredentialError):install(self.base,Fernet.generate_key().decode())
            save.assert_not_called()
    def test_new_bundle_uses_protected_key_without_dialog(self):
        install(self.base,self.key);self.bundle['oauth']=oauth('next');self.write_bundle();self.assertFalse(ready(self.base))
        with patch('tkinter.filedialog.askopenfilename') as ask:
            ensure_personal(self.base,None);ask.assert_not_called()
        self.assertTrue(ready(self.base))
    def test_malformed_payload_rejected_before_write(self):
        for invalid in ([],{'version':2,'oauth':oauth(),'logins':{'A':None}}):
            self.bundle=invalid;self.write_bundle()
            with self.assertRaises(CredentialError):install(self.base,self.key)
        self.assertFalse((self.base/'data').exists())
    def test_unsafe_oauth_endpoint_rejected(self):
        self.bundle['oauth']['installed']['token_uri']='https://example.com/token';self.write_bundle()
        with self.assertRaises(GmailError):install(self.base,self.key)
        self.assertFalse((self.base/'data').exists())

class ClientTests(unittest.TestCase):
    def test_same_client_preserves_grant(self):
        s={'credentials':{'client_id':oauth()['installed']['client_id'],'refresh_token':'dummy-refresh'}}
        check_client(s,oauth());self.assertEqual(s['credentials']['refresh_token'],'dummy-refresh');self.assertEqual(s['credentials']['token_uri'],'https://oauth2.googleapis.com/token')
    def test_changed_client_requires_consent_without_token_modification(self):
        s={'credentials':{'client_id':'old','refresh_token':'dummy'}};before=json.dumps(s)
        with self.assertRaisesRegex(GmailError,'CLIENT_CHANGED'):check_client(s,oauth())
        self.assertEqual(json.dumps(s),before)
    def test_changed_client_and_scope_repair(self):
        with patch('gmail_auth.service',side_effect=GmailError('SCOPE_MISSING','test')),patch('gmail_auth.load',return_value=None),patch('gmail_auth.connect',return_value=('api','mail')) as connect:
            self.assertEqual(ensure_service(Path('data'),'C'),('api','mail'));connect.assert_called_once()
        with patch('gmail_auth.service',side_effect=GmailError('CLIENT_CHANGED','test')),patch('gmail_auth.connect') as connect:
            with self.assertRaisesRegex(GmailError,'CLIENT_CHANGED'):ensure_service(Path('data'),'C')
            connect.assert_not_called()
    def test_web_oauth_not_accepted(self):
        with self.assertRaises(GmailError):validate_client({'web':oauth()['installed']})

if __name__=='__main__':unittest.main()
