"""Known manual-auth failures never cause repeated unattended submissions."""
from datetime import timedelta
import json
from unittest.mock import Mock, patch

from gmail_auth import GmailError
from login_credentials import AuthenticationRequired
from monthly_attention import blocked
from monthly_schedule import due_slot
from test_monthly_runner_integration import MonthlyRunnerFixture


class MonthlyAuthenticationRecovery(MonthlyRunnerFixture):
    def test_manual_asset_auth_holds_pdf_and_retries_until_explicit_successful_test(self):
        acquire = self.acquire

        def manual(playwright, account, stage=None, **options):
            if account == 'A' and not options['documents']:
                self.calls.append(('blocked-asset', account))
                raise AuthenticationRequired('Synthetic manual state')
            return acquire(playwright, account, stage, **options)

        result = self.run_monthly(acquire=manual)
        self.assertNotEqual(result['exit_code'], 0)
        self.assertEqual(result['accounts']['A']['asset_status'], 'needs-attention')
        self.assertEqual(result['accounts']['A']['pdf_status'], 'blocked')
        self.assertNotIn(('pdf', 'A'), self.calls)
        self.calls.clear(); self.auth_calls.clear()
        self.current += timedelta(minutes=1)
        retry = self.run_monthly()
        self.assertNotEqual(retry['exit_code'], 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.auth_calls, [])
        self.current += timedelta(minutes=1)
        tested = self.run_monthly(test=True)
        self.assertEqual(tested['exit_code'], 0)
        self.assertFalse(blocked(self.base, due_slot(self.current), 'A', 'Example A'))
        self.calls.clear(); self.auth_calls.clear()
        self.current += timedelta(minutes=1)
        self.assertEqual(self.run_monthly()['exit_code'], 0)
        self.assertEqual(self.calls, [('asset', 'A'), ('pdf', 'A')])
        self.assertEqual(self.protected_rows(), self.before)

    def test_gmail_expired_consent_is_blocked_before_browser_and_not_repeated(self):
        def authenticate(directory, account):
            if account == 'A':
                self.auth_calls.append(account)
                raise GmailError('NEEDS_CONSENT', 'Synthetic expired consent')
            return self.authenticate(directory, account)

        result = self.run_monthly(authenticate=authenticate)
        self.assertNotEqual(result['exit_code'], 0)
        self.assertEqual(self.auth_calls.count('A'), 1)
        self.assertEqual(self.calls, [('asset', a) for a in 'BCD'] + [('pdf', a) for a in 'BCD'])
        self.calls.clear(); self.auth_calls.clear()
        self.current += timedelta(minutes=1)
        self.assertNotEqual(self.run_monthly(authenticate=authenticate)['exit_code'], 0)
        self.assertEqual(self.auth_calls, [])
        self.assertEqual(self.calls, [])

    def test_pdf_manual_state_keeps_complete_assets_and_skips_scheduled_relogin(self):
        acquire = self.acquire

        def manual(playwright, account, stage=None, **options):
            if account == 'C' and options['documents']:
                self.calls.append(('blocked-pdf', account))
                raise AuthenticationRequired('Synthetic CAPTCHA')
            return acquire(playwright, account, stage, **options)

        result = self.run_monthly(acquire=manual)
        self.assertEqual(result['accounts']['C']['asset_status'], 'complete')
        self.assertEqual(result['accounts']['C']['pdf_status'], 'blocked')
        self.assertEqual(len(self.observations()), 4)
        self.calls.clear(); self.auth_calls.clear()
        self.current += timedelta(minutes=1)
        retry = self.run_monthly()
        self.assertNotEqual(retry['exit_code'], 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.auth_calls, [])

    def test_corrupt_hold_is_retained_and_failure_has_a_report_without_auth(self):
        from monthly_attention import save_attention
        path = save_attention(self.base, due_slot(self.current), 'A', 'Example A')
        content = json.loads(path.read_text(encoding='utf-8'))
        content['owner'] = 'Modified'
        path.write_text(json.dumps(content), encoding='utf-8')
        before = path.read_bytes()
        result = self.run_monthly()
        self.assertNotEqual(result['exit_code'], 0)
        self.assertEqual(self.auth_calls, [])
        self.assertEqual(path.read_bytes(), before)
        self.assertIsNotNone(result['run_path'])

    def test_confirmed_post_owner_mismatch_is_typed_manual_auth_failure(self):
        from post_identity import verify
        page = Mock(url='https://plus.e-shishobako.ne.jp/fixture/user-delivery')
        page.frames = []
        page.locator.return_value.inner_text.return_value = 'Example B 様'
        with patch('family_profile.OWNERS', {'A': 'Example A', 'B': 'Example B'}):
            with self.assertRaises(AuthenticationRequired):
                verify(page, 'Example A', clock=lambda: 0)
