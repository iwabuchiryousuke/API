import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from data_backup import backup_before_upgrade
from family_profile import configure, OWNERS
from ledger import Store
from login_credentials import CredentialError
from update_workflow import Cancelled, Workflow


class UpgradeSafety(unittest.TestCase):
    def test_complete_backup_preserves_settings_and_consistent_database(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            store = Store(base / 'data')
            store.rename('C', 'Custom label')
            store.set_owner('C', 'Custom identity')
            private = base / 'data' / 'private' / 'login_C.dat'
            private.parent.mkdir()
            private.write_bytes(b'opaque-encrypted-settings\x00\xff')
            store.db.execute('PRAGMA journal_mode=WAL')
            result = backup_before_upgrade(base, '3.5.12')
            self.assertEqual((result / 'data/private/login_C.dat').read_bytes(), private.read_bytes())
            copy = sqlite3.connect(result / 'data/ledger.sqlite3')
            self.assertEqual(copy.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(copy.execute("SELECT owner_name FROM accounts WHERE id='C'").fetchone()[0], 'Custom identity')
            copy.close()
            metadata = json.loads((result / 'manifest.json').read_text(encoding='utf-8'))
            self.assertTrue(metadata['complete'])
            for name, digest in metadata['sha256'].items():
                self.assertEqual(hashlib.sha256((result / 'data' / name).read_bytes()).hexdigest(), digest)
            self.assertEqual(backup_before_upgrade(base, '3.5.12'), result)
            self.assertEqual(len(list((base / 'backups').iterdir())), 1)
            store.close()

    def test_failed_backup_cannot_be_mistaken_for_completed_upgrade(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / 'data/private').mkdir(parents=True)
            source = base / 'data/private/settings.secure'
            source.write_bytes(b'opaque-original')
            with patch('data_backup.shutil.copy2', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):backup_before_upgrade(base, '3.5.12')
            self.assertEqual(list((base / 'backups').iterdir()), [])
            self.assertEqual(source.read_bytes(), b'opaque-original')

    def test_saved_identities_are_never_replaced_by_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            for account in store.accounts():store.set_owner(account['id'], 'Custom ' + account['id'])
            with patch.object(store, 'backup') as backup:
                configure(store)
                backup.assert_not_called()
            self.assertEqual([a['owner_name'] for a in store.accounts()], ['Custom ' + a for a in 'ABCDE'])
            store.close()

    def test_corrupt_previous_backup_is_kept_and_replaced_by_fresh_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);(base/'data/private').mkdir(parents=True)
            (base/'data/private/setting.secure').write_bytes(b'original')
            first=backup_before_upgrade(base,'3.5.12')
            (first/'data/private/setting.secure').write_bytes(b'corrupt')
            second=backup_before_upgrade(base,'3.5.12')
            self.assertNotEqual(first,second)
            self.assertEqual((second/'data/private/setting.secure').read_bytes(),b'original')
            self.assertTrue(first.exists())

    def test_only_missing_identities_are_initialized_once(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            store.set_owner('C', 'Custom C')
            with patch.object(store, 'backup') as backup:
                configure(store);configure(store)
                backup.assert_called_once()
            owners = {a['id']: a['owner_name'] for a in store.accounts()}
            self.assertEqual(owners['C'], 'Custom C')
            self.assertEqual(owners['A'], OWNERS['A'])
            store.close()

    def test_cancel_during_last_account_is_cancellation_without_commit(self):
        with tempfile.TemporaryDirectory() as folder:
            stopped = [False]
            def collect(*args):
                stopped[0] = True
                raise CredentialError('中止しました。')
            with self.assertRaises(Cancelled):
                Workflow(folder, cancelled=lambda: stopped[0]).run(
                    collect=collect, accounts=('C',), review=lambda *a: True)
            self.assertFalse((Path(folder) / 'output').exists())

    def test_navigation_diagnostics_excludes_session_urls_and_form_content(self):
        from navigation_diagnostics import save_navigation_failure
        page=Mock();page.is_closed.return_value=False;page.frames=[page]
        page.url='https://m.sbisec.co.jp/account?session=PRIVATE_SESSION_VALUE'
        rows=[dict(labels=['口座管理'],domestic=False,visible=True,enabled=True,href='javascript:PRIVATE_SESSION_VALUE'),
              dict(labels=['PRIVATE_PASSWORD_VALUE'],domestic=False,visible=True,enabled=True,href='')]
        with tempfile.TemporaryDirectory() as folder, patch('sbi_navigation.scan',return_value=rows):
            path=save_navigation_failure(Mock(pages=[page]),folder,'C','電子ポストへ移動・名義確認')
            output=path.read_text(encoding='utf-8')
            self.assertNotIn('PRIVATE_',output)
            self.assertIn('m.sbisec.co.jp',output)
            self.assertIn('口座管理',output)
            page.locator.assert_not_called()

    def test_cancel_before_identity_or_otp_does_not_touch_page(self):
        from post_identity import verify
        from auto_login import submit_otp
        page=Mock()
        with self.assertRaisesRegex(CredentialError,'中止'):
            verify(page,'Example Name',cancelled=lambda:True)
        with self.assertRaisesRegex(CredentialError,'中止'):
            submit_otp(page,[Mock()],cancelled=lambda:True)
        page.locator.assert_not_called()


class AutomaticAcquisition(unittest.TestCase):
    def execute(self, documents=True, identity_failure=False):
        from contextlib import ExitStack
        from auto_login import run_one
        page = Mock()
        page.url = 'https://m.sbisec.co.jp/account'
        page.is_closed.return_value = False
        page.frames = [page]
        page.locator.return_value.inner_text.return_value = 'Custom C 様 ログアウト'
        playwright = Mock()
        browser = playwright.chromium.launch.return_value
        context = browser.new_context.return_value
        context.new_page.return_value = page
        context.pages = [page]
        store = Mock()
        store.accounts.return_value = [{'id':'C', 'owner_name':'Custom C'}]
        mocks = {
            'auto_login.Store': Mock(side_effect=AssertionError('must use staging account settings')),
            'auto_login.autofill_login': Mock(return_value=True),
            'auto_login._login_page': Mock(return_value=page),
            'auto_login.recent_ids': Mock(return_value=set()),
            'auto_login.submit_login': Mock(),
            'auto_login.visible': Mock(return_value=[]),
            'auto_login.otp_fields': Mock(return_value=[]),
            'sbi_views.landing': Mock(return_value=page),
            'auto_login.authenticated_page': Mock(return_value=page),
            'auto_login.open_electronic_post': Mock(return_value=page),
            'sbi_views.verify_post_owner': Mock(),
            'auto_login.collect': Mock(return_value=(2,0,0)),
            'sbi_views.capture_account': Mock(side_effect=CredentialError('別口座の名義') if identity_failure else None, return_value=True),
            'sbi_views.capture_status': Mock(return_value={'available':['cash'], 'missing':[], 'usd_cash_observed':True}),
            'acquisition_journal.persist_capture': Mock(return_value=None),
        }
        with ExitStack() as stack:
            for name, value in mocks.items():stack.enter_context(patch(name,value))
            if identity_failure:
                with self.assertRaisesRegex(CredentialError, '別口座'):run_one(playwright,'C',store,gmail_session=(Mock(),'example@example.test'),documents=documents)
                result = None
            else:
                result = run_one(playwright,'C',store,gmail_session=(Mock(),'example@example.test'),documents=documents)
        store.close.assert_not_called()
        browser.close.assert_called_once()
        self.assertEqual(mocks['sbi_views.capture_account'].call_args.kwargs['owner'],'Custom C')
        self.assertTrue(mocks['sbi_views.capture_account'].call_args.kwargs['identity_verified'])
        return mocks, result

    def test_assets_can_be_fetched_without_electronic_post(self):
        mocks, result = self.execute(documents=False)
        mocks['auto_login.authenticated_page'].assert_called_once()
        mocks['auto_login.open_electronic_post'].assert_not_called()
        mocks['auto_login.collect'].assert_not_called()
        mocks['sbi_views.verify_post_owner'].assert_not_called()
        self.assertFalse(result['pdf_collected'])
        self.assertTrue(result['assets_observed'])

    def test_normal_pdf_workflow_keeps_post_identity_check(self):
        mocks, result = self.execute()
        mocks['sbi_views.verify_post_owner'].assert_called_once()
        mocks['auto_login.collect'].assert_called_once()
        self.assertEqual(result['saved'],2)

    def test_asset_identity_failure_is_never_a_success_warning(self):
        self.execute(identity_failure=True)


if __name__ == '__main__':unittest.main()
