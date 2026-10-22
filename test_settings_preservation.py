"""No real accounts or DPAPI: preservation and atomic settings regressions."""
import copy
import json
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack,contextmanager
from pathlib import Path
from unittest.mock import MagicMock,patch

import gmail_auth
import initial_setup
from login_credentials import CredentialError


def oauth(client='configured'):
    return {'installed':{'client_id':client+'.apps.googleusercontent.com',
        'client_secret':'dummy-only','auth_uri':'https://accounts.google.com/o/oauth2/auth',
        'token_uri':'https://oauth2.googleapis.com/token'}}


@contextmanager
def google_modules(recipient):
    """Tiny dependency doubles avoid any browser, network or Google credentials."""
    creds=MagicMock();creds.valid=True;creds.refresh_token='dummy'
    creds.has_scopes.return_value=True;creds.to_json.return_value='{}'
    api=MagicMock();api.users().getProfile().execute.return_value={'emailAddress':recipient}
    factory=MagicMock();factory.from_authorized_user_info.return_value=creds
    flow=MagicMock();flow.run_local_server.return_value=creds
    installed=MagicMock();installed.from_client_config.return_value=flow
    modules={name:types.ModuleType(name) for name in ('google','google.oauth2',
        'google.oauth2.credentials','google.auth','google.auth.transport',
        'google.auth.transport.requests','googleapiclient','googleapiclient.discovery',
        'google_auth_oauthlib','google_auth_oauthlib.flow')}
    modules['google.oauth2.credentials'].Credentials=factory
    modules['google.auth.transport.requests'].Request=MagicMock()
    modules['googleapiclient.discovery'].build=MagicMock(return_value=api)
    modules['google_auth_oauthlib.flow'].InstalledAppFlow=installed
    with patch.dict(sys.modules,modules):yield creds,api,flow


class GmailSettingsPreservation(unittest.TestCase):
    def settings(self):
        return {'recipient':'custom.user@example.com','credentials':{
            'client_id':oauth()['installed']['client_id'],'client_secret':'old-dummy',
            'refresh_token':'dummy-refresh','token_uri':'https://oauth2.googleapis.com/token'}}

    def test_saved_recipient_is_used_instead_of_family_default(self):
        setting=self.settings();before=copy.deepcopy(setting)
        def load(directory,name):return setting if name=='gmail_C' else oauth()
        with google_modules(setting['recipient']) as (_,api,_),patch('gmail_auth.load',side_effect=load),patch('gmail_auth.save') as save:
            result=gmail_auth.service('unused','C')
        self.assertEqual(result,(api,setting['recipient']))
        self.assertEqual(setting,before) # Client refresh edits a detached copy only.
        self.assertEqual(save.call_args.args[2]['recipient'],setting['recipient'])

    def test_actual_mailbox_mismatch_never_reconnects_or_persists(self):
        setting=self.settings();before=copy.deepcopy(setting)
        def load(directory,name):return setting if name=='gmail_C' else oauth()
        with google_modules('other@example.com'),patch('gmail_auth.load',side_effect=load),patch('gmail_auth.save') as save,patch('gmail_auth.connect') as connect:
            with self.assertRaisesRegex(gmail_auth.GmailError,'MAILBOX_MISMATCH'):
                gmail_auth.ensure_service('unused','C')
        save.assert_not_called();connect.assert_not_called();self.assertEqual(setting,before)

    def test_scope_repair_keeps_custom_recipient(self):
        with patch('gmail_auth.service',side_effect=gmail_auth.GmailError('SCOPE_MISSING','test')),patch('gmail_auth.load',return_value=self.settings()),patch('gmail_auth.connect',return_value=('api','mail')) as connect:
            self.assertEqual(gmail_auth.ensure_service('unused','C'),('api','mail'))
        connect.assert_called_once_with('unused','C',recipient=self.settings()['recipient'])

    def test_bad_saved_recipient_is_not_replaced_by_default(self):
        with patch('gmail_auth.load',return_value={'recipient':'not an address'}):
            with self.assertRaisesRegex(gmail_auth.GmailError,'MAILBOX_CONFIG'):
                gmail_auth.recipient_for('unused','A')

    def test_client_resolution_does_not_write_settings(self):
        with patch('gmail_auth.load',return_value=oauth()),patch('gmail_auth.save') as save:
            self.assertEqual(gmail_auth.client_config('unused'),oauth())
        save.assert_not_called()

    def test_failed_oauth_does_not_replace_client_or_grant(self):
        with google_modules('different@example.com'),patch('gmail_auth.load',return_value=self.settings()),patch('gmail_auth.client_config',return_value=oauth('candidate')),patch('gmail_auth.save') as save:
            with self.assertRaisesRegex(gmail_auth.GmailError,'MAILBOX_MISMATCH'):
                gmail_auth.connect('unused','C')
        save.assert_not_called()

    def test_connection_write_failure_restores_both_files_and_temps(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp);private=directory/'private';private.mkdir()
            (private/'gmail_client.secure').write_bytes(b'old-client')
            (private/'gmail_C.secure').write_bytes(b'old-grant')
            (private/'gmail_client.tmp').write_bytes(b'existing-temp')
            before={p.name:p.read_bytes() for p in private.iterdir()}
            def fail(directory,name,value):
                (private/(name+'.secure')).write_bytes(b'new-write')
                if name=='gmail_C':
                    (private/(name+'.tmp')).write_bytes(b'incomplete-write')
                    raise OSError('simulated write failure')
            with patch('gmail_auth.save',side_effect=fail):
                with self.assertRaises(OSError):gmail_auth.save_connection(directory,'C',oauth(),self.settings())
            self.assertEqual(before,{p.name:p.read_bytes() for p in private.iterdir()})


class InitialSetupPreservation(unittest.TestCase):
    def setUp(self):
        from cryptography.fernet import Fernet
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name)
        self.private=self.base/'data/private';self.private.mkdir(parents=True)
        self.key=Fernet.generate_key()
        bundle={'version':2,'oauth':oauth('older-bundle'),
            'logins':{a:{'username':'dummy','password':'dummy'} for a in 'ABCD'}}
        (self.base/'personal_setup.enc').write_bytes(Fernet(self.key).encrypt(json.dumps(bundle).encode()))
        def save(directory,name,value):
            (self.private/(name+'.secure')).write_text(json.dumps(value))
        def load(directory,name):
            p=self.private/(name+'.secure')
            return json.loads(p.read_text()) if p.exists() else None
        def login(directory,account,username,password):
            (self.private/f'login_{account}.dat').write_bytes(b'dummy-login')
        self.stack=ExitStack()
        self.save=self.stack.enter_context(patch('initial_setup.save',side_effect=save))
        self.stack.enter_context(patch('initial_setup.load',side_effect=load))
        self.stack.enter_context(patch('initial_setup.save_credentials',side_effect=login))
        self.stack.enter_context(patch('initial_setup.has_credentials',side_effect=lambda d,a:(self.private/f'login_{a}.dat').exists()))

    def tearDown(self):self.stack.close();self.tmp.cleanup()

    def test_new_bundle_keeps_existing_client_and_passwords(self):
        original=json.dumps(oauth('user-chosen')).encode()
        (self.private/'gmail_client.secure').write_bytes(original)
        (self.private/'login_A.dat').write_bytes(b'user-changed-password')
        initial_setup.install(self.base,self.key.decode())
        self.assertEqual((self.private/'gmail_client.secure').read_bytes(),original)
        self.assertEqual((self.private/'login_A.dat').read_bytes(),b'user-changed-password')
        self.assertTrue(initial_setup.ready(self.base))
        self.assertNotIn('gmail_client',[call.args[1] for call in self.save.call_args_list])

    def test_invalid_existing_client_stops_without_changes(self):
        (self.private/'gmail_client.secure').write_text('{}')
        before={p.name:p.read_bytes() for p in self.private.iterdir()}
        with self.assertRaises(CredentialError):initial_setup.install(self.base,self.key.decode())
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.private.iterdir()})

    def test_partial_initial_install_removes_new_files_and_temp_artifacts(self):
        (self.private/'login_A.dat').write_bytes(b'existing-login')
        (self.private/'provision_key.tmp').write_bytes(b'existing-temp')
        before={p.name:p.read_bytes() for p in self.private.iterdir()}
        original=self.save.side_effect
        def fail(directory,name,value):
            original(directory,name,value)
            if name=='provision_key':
                (self.private/'personal_setup.tmp').write_bytes(b'incomplete-write')
                raise OSError('simulated write failure')
        with patch('initial_setup.save',side_effect=fail):
            with self.assertRaises(OSError):initial_setup.install(self.base,self.key.decode())
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.private.iterdir()})


if __name__=='__main__':unittest.main()
