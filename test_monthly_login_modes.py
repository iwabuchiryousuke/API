"""Summary-only acquisition and headless login fail closed at owner checks."""
from contextlib import ExitStack,redirect_stdout
import base64
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from auto_login import AcquisitionError,IdentityUnavailable,authenticated_page,run_one
from gmail_auth import GmailError
from login_credentials import (CredentialError,AuthenticationRequired,load_credentials,
                               credential_path,_MAGIC)


OWNER='Anonymous C'
ENTRY='https://site3.sbisec.co.jp/account-summary?session=anonymous-fixture'


class MonthlyLoginModes(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        self.page=Mock();self.page.url=ENTRY;self.page.frames=[self.page]
        self.page.is_closed.return_value=False
        self.page.locator.return_value.inner_text.return_value=OWNER+' 様 ログアウト 口座管理'
        self.page.locator.return_value.count.return_value=0
        self.pw=Mock();self.browser=self.pw.chromium.launch.return_value
        self.context=self.browser.new_context.return_value
        self.context.pages=[self.page];self.context.new_page.return_value=self.page
        self.page.context=self.context
        self.store=Mock();self.store.accounts.return_value=[{'id':'C','owner_name':OWNER}]
        self.messages=[];self.events=[]
        self.summary={'complete':True,'missing':[],
            'values':{'summary_stocks':100,'cash_total':200},
            'observed_at':'2026-10-23T16:00:12+09:00','displayed_at':None}
        self.mocks={
            'auto_login.BASE':self.base,
            'auto_login.autofill_login':Mock(return_value=True),
            'auto_login._login_page':Mock(return_value=self.page),
            'auto_login.recent_ids':Mock(return_value=set()),
            'auto_login.submit_login':Mock(),
            'auto_login.visible':Mock(return_value=[]),
            'auto_login.otp_fields':Mock(return_value=[]),
            'sbi_views.landing':Mock(return_value=self.page),
            'auto_login.authenticated_page':Mock(return_value=self.page),
            'auto_login.open_electronic_post':Mock(return_value=self.page),
            'sbi_views.verify_post_owner':Mock(),
            'auto_login.collect':Mock(return_value=(2,0,0)),
            'sbi_views.select_asset_page':Mock(return_value=self.page),
            'sbi_views.capture_account':Mock(return_value=True),
            'sbi_views.capture_status':Mock(return_value={'complete':True,'available':['cash'],
                'missing':[],'usd_cash_observed':True}),
            'monthly_summary.capture_summary':Mock(return_value=self.summary),
            'acquisition_journal.persist_capture':Mock(return_value=None),
            'post_identity.save_failure_evidence':Mock(),
        }

    def execute(self,**options):
        with ExitStack() as stack:
            for name,value in self.mocks.items():stack.enter_context(patch(name,value))
            return run_one(self.pw,'C',self.store,gmail_session=(Mock(),'anonymous@example.test'),
                           progress=self.messages.append,**options)

    def assert_no_measurement(self):
        self.mocks['monthly_summary.capture_summary'].assert_not_called()
        self.mocks['sbi_views.capture_account'].assert_not_called()
        self.mocks['acquisition_journal.persist_capture'].assert_not_called()

    def test_summary_mode_requires_assets_enabled_and_documents_disabled(self):
        for options in ({'documents':True,'capture_assets':True},
                        {'documents':False,'capture_assets':False},
                        {'documents':True,'capture_assets':False}):
            with self.subTest(options=options),self.assertRaises(CredentialError):
                self.execute(summary_only=True,**options)
        self.pw.chromium.launch.assert_not_called();self.assert_no_measurement()

    def test_summary_success_does_not_enter_pdf_or_full_asset_collection(self):
        result=self.execute(documents=False,summary_only=True)
        self.assertIs(result['monthly_summary'],self.summary)
        self.assertTrue(result['assets_observed']);self.assertEqual(result['assets_missing'],[])
        self.assertFalse(result['pdf_collected']);self.assertEqual(result['saved'],0)
        self.pw.chromium.launch.assert_called_once_with(channel='msedge',headless=False)
        self.mocks['monthly_summary.capture_summary'].assert_called_once()
        kwargs=self.mocks['monthly_summary.capture_summary'].call_args.kwargs
        self.assertEqual(kwargs['owner'],OWNER);self.assertTrue(kwargs['identity_verified'])
        self.mocks['auto_login.open_electronic_post'].assert_not_called()
        self.mocks['auto_login.collect'].assert_not_called()
        self.mocks['sbi_views.capture_account'].assert_not_called()
        self.mocks['acquisition_journal.persist_capture'].assert_called_once()
        self.browser.close.assert_called_once();self.store.close.assert_not_called()

    def test_headless_summary_forwards_edge_option(self):
        self.execute(documents=False,summary_only=True,headless=True)
        self.pw.chromium.launch.assert_called_once_with(channel='msedge',headless=True)

    def test_gmail_manual_code_survives_context_wrapper_and_blocks_retry(self):
        from monthly_attention import attention_required
        self.mocks['auto_login.recent_ids'].side_effect=GmailError('API_FORBIDDEN','Gmail APIの設定を確認してください。')
        with self.assertRaises(GmailError) as error:self.execute(documents=False,summary_only=True)
        self.assertEqual(error.exception.code,'API_FORBIDDEN')
        self.assertEqual(error.exception.acquisition_code,'SBI_AUTHENTICATION_FAILED')
        self.assertEqual(error.exception.account,'C')
        self.assertEqual(str(error.exception).count('[API_FORBIDDEN]'),1)
        self.assertTrue(attention_required(error.exception));self.assert_no_measurement()

    def test_gmail_rate_limit_keeps_retryable_code_after_context_wrapper(self):
        from monthly_attention import attention_required
        self.mocks['auto_login.recent_ids'].side_effect=GmailError('API_LIMIT','時間をおいて再実行してください。')
        with self.assertRaises(GmailError) as error:self.execute(documents=False,summary_only=True)
        self.assertEqual(error.exception.code,'API_LIMIT')
        self.assertFalse(attention_required(error.exception));self.assert_no_measurement()

    def test_actual_mail_api_forbidden_is_classified_without_exposing_server_body(self):
        class FakeAPIError(Exception):
            resp=Mock(status=403)
        self.mocks['auto_login.recent_ids'].side_effect=FakeAPIError('https://example.invalid/anonymous-token raw response')
        with self.assertRaises(GmailError) as error:self.execute(documents=False,summary_only=True)
        self.assertEqual(error.exception.code,'API_FORBIDDEN')
        self.assertEqual(error.exception.acquisition_code,'SBI_AUTHENTICATION_FAILED')
        self.assertNotIn('https://',str(error.exception));self.assertNotIn('raw response',str(error.exception))
        self.assert_no_measurement()

    def test_launch_failure_has_fixed_code_without_raw_browser_details(self):
        self.pw.chromium.launch.side_effect=RuntimeError('anonymous browser detail https://example.invalid/session')
        with self.assertRaises(AcquisitionError) as error:self.execute(documents=False,summary_only=True)
        self.assertEqual(error.exception.code,'EDGE_LAUNCH_FAILED')
        self.assertEqual(error.exception.acquisition_code,'EDGE_LAUNCH_FAILED')
        self.assertEqual(error.exception.account,'C')
        self.assertNotIn('https://',str(error.exception));self.assert_no_measurement()

    def test_unknown_network_failure_has_fixed_stage_without_raw_url(self):
        self.page.goto.side_effect=RuntimeError('https://example.invalid/session/anonymous')
        with self.assertRaises(AcquisitionError) as error:self.execute(documents=False,summary_only=True)
        self.assertEqual(error.exception.code,'SBI_SITE_UNAVAILABLE')
        self.assertNotIn('https://',str(error.exception));self.assert_no_measurement()

    def test_browser_close_failure_does_not_mask_original_gmail_classification(self):
        self.browser.close.side_effect=RuntimeError('anonymous close failure')
        self.mocks['auto_login.recent_ids'].side_effect=GmailError('API_FORBIDDEN','設定を確認してください。')
        with self.assertRaises(GmailError) as error:self.execute(documents=False,summary_only=True)
        self.assertEqual(error.exception.code,'API_FORBIDDEN')
        self.assertIn('Edgeの終了確認',self.messages[-1]);self.assert_no_measurement()

    def test_browser_close_failure_keeps_successful_summary_and_warns(self):
        self.browser.close.side_effect=RuntimeError('anonymous close failure')
        result=self.execute(documents=False,summary_only=True)
        self.assertTrue(result['assets_observed']);self.assertIn('Edgeの終了確認',self.messages[-1])

    def test_empty_summary_uses_summary_code_after_journal_phase(self):
        self.summary.update(complete=False,missing=['summary_stocks','cash_total'],values={})
        with self.assertRaises(AcquisitionError) as error:self.execute(documents=False,summary_only=True)
        self.assertEqual(error.exception.code,'SUMMARY_NOT_OBSERVED')
        self.assertEqual(error.exception.acquisition_code,'SUMMARY_NOT_OBSERVED')

    def test_explicit_summary_conflict_code_is_not_replaced_by_generic_stage(self):
        self.mocks['monthly_summary.capture_summary'].side_effect=AcquisitionError('SUMMARY_VALUES_CONFLICT','表示額が整合しません。')
        with self.assertRaises(AcquisitionError) as error:self.execute(documents=False,summary_only=True)
        self.assertEqual(error.exception.code,'SUMMARY_VALUES_CONFLICT')
        self.assertEqual(error.exception.acquisition_code,'SUMMARY_VALUES_CONFLICT')
        self.mocks['acquisition_journal.persist_capture'].assert_not_called()

    def test_partial_summary_keeps_unknown_stock_in_missing_list(self):
        self.summary.update(complete=False,missing=['summary_stocks'],values={'cash_total':200})
        result=self.execute(documents=False,summary_only=True)
        self.assertFalse(result['assets_observed'])
        self.assertEqual(result['assets_missing'],['summary_stocks'])
        self.assertNotIn('summary_stocks',result['monthly_summary']['values'])

    def test_typed_owner_unavailable_can_verify_epost_for_summary_without_collecting_pdf(self):
        self.mocks['auto_login.authenticated_page'].side_effect=IdentityUnavailable('名義未表示')
        self.mocks['auto_login.open_electronic_post'].side_effect=lambda *a,**k:self.events.append('post') or self.page
        self.mocks['sbi_views.verify_post_owner'].side_effect=lambda *a,**k:self.events.append('verify')
        self.mocks['sbi_views.select_asset_page'].side_effect=lambda *a,**k:self.events.append('return-sbi') or self.page
        self.mocks['monthly_summary.capture_summary'].side_effect=lambda *a,**k:self.events.append('summary') or self.summary
        result=self.execute(documents=False,summary_only=True,headless=True)
        self.assertEqual(self.events,['post','verify','return-sbi','return-sbi','summary'])
        self.assertTrue(result['assets_observed'])
        self.assertEqual(self.mocks['auto_login.open_electronic_post'].call_args.kwargs['manual_seconds'],0)
        self.assertEqual(self.mocks['sbi_views.verify_post_owner'].call_args.args[1],OWNER)
        for call in self.mocks['sbi_views.select_asset_page'].call_args_list:
            self.assertEqual(call.kwargs['entry_url'],ENTRY);self.assertTrue(call.kwargs['identity_verified'])
        self.mocks['auto_login.collect'].assert_not_called()
        self.mocks['sbi_views.capture_account'].assert_not_called()

    def test_owner_unavailable_does_not_enable_epost_fallback_for_ordinary_assets(self):
        self.mocks['auto_login.authenticated_page'].side_effect=IdentityUnavailable('名義未表示')
        with self.assertRaisesRegex(CredentialError,'名義未表示'):self.execute(documents=False)
        self.mocks['auto_login.open_electronic_post'].assert_not_called();self.assert_no_measurement()

    def test_explicit_owner_mismatch_never_enters_summary_fallback(self):
        self.mocks['auto_login.authenticated_page'].side_effect=AuthenticationRequired('選択口座と異なるログイン名義')
        with self.assertRaisesRegex(AuthenticationRequired,'異なるログイン名義') as error:
            self.execute(documents=False,summary_only=True)
        self.assertEqual(error.exception.code,'MANUAL_AUTH_REQUIRED');self.assertFalse(error.exception.retryable)
        self.mocks['auto_login.open_electronic_post'].assert_not_called();self.assert_no_measurement()
        self.browser.close.assert_called_once()

    def test_cancelled_identity_check_never_enters_summary_fallback(self):
        self.mocks['auto_login.authenticated_page'].side_effect=CredentialError('中止しました。')
        with self.assertRaisesRegex(CredentialError,'中止'):
            self.execute(documents=False,summary_only=True)
        self.mocks['auto_login.open_electronic_post'].assert_not_called();self.assert_no_measurement()

    def test_epost_owner_mismatch_blocks_summary_and_journal(self):
        self.mocks['auto_login.authenticated_page'].side_effect=IdentityUnavailable('名義未表示')
        self.mocks['sbi_views.verify_post_owner'].side_effect=CredentialError('電子ポスト名義が別口座です。')
        with self.assertRaisesRegex(CredentialError,'別口座'):
            self.execute(documents=False,summary_only=True)
        self.mocks['auto_login.collect'].assert_not_called();self.assert_no_measurement()
        self.browser.close.assert_called_once()

    def test_cancel_after_epost_owner_verification_blocks_measurement(self):
        stopped=[False]
        self.mocks['auto_login.authenticated_page'].side_effect=IdentityUnavailable('名義未表示')
        def cancel(*args,**kwargs):stopped[0]=True
        self.mocks['sbi_views.verify_post_owner'].side_effect=cancel
        with self.assertRaisesRegex(CredentialError,'中止'):
            self.execute(documents=False,summary_only=True,cancelled=lambda:stopped[0])
        self.mocks['auto_login.collect'].assert_not_called();self.assert_no_measurement()

    def test_unstable_summary_is_fatal_without_full_asset_fallback(self):
        self.mocks['monthly_summary.capture_summary'].side_effect=RuntimeError('unstable fixture')
        with self.assertRaisesRegex(CredentialError,'安定した株式評価額'):
            self.execute(documents=False,summary_only=True)
        self.mocks['sbi_views.capture_account'].assert_not_called()
        self.mocks['acquisition_journal.persist_capture'].assert_not_called()

    def test_known_missing_credentials_preserve_permanent_type_through_run_wrapper(self):
        self.mocks['auto_login.autofill_login'].side_effect=AuthenticationRequired('保存済みログイン情報がありません。')
        with self.assertRaises(AuthenticationRequired) as error:self.execute(documents=False,summary_only=True)
        self.assertEqual(error.exception.code,'MANUAL_AUTH_REQUIRED');self.assertFalse(error.exception.retryable)
        self.assertIn('口座C／',str(error.exception));self.assert_no_measurement()
        self.mocks['auto_login.open_electronic_post'].assert_not_called();self.browser.close.assert_called_once()

    def test_explicit_captcha_or_access_restriction_blocks_automatic_retry(self):
        for text in ('ロボットではないことを確認してください','CAPTCHA','アクセスが制限されています'):
            with self.subTest(text=text):
                self.page.locator.return_value.inner_text.return_value=text
                with self.assertRaises(AuthenticationRequired) as error:self.execute(documents=False,summary_only=True)
                self.assertEqual(error.exception.code,'MANUAL_AUTH_REQUIRED');self.assertFalse(error.exception.retryable)
        self.assert_no_measurement();self.mocks['auto_login.open_electronic_post'].assert_not_called()

    def test_unknown_navigation_failure_does_not_become_permanent_auth_error(self):
        self.mocks['auto_login.autofill_login'].return_value=False
        with patch('epost_collector.login_diagnostics',return_value='表示待機中'):
            with self.assertRaises(CredentialError) as error:self.execute(documents=False,summary_only=True)
        self.assertNotIsInstance(error.exception,AuthenticationRequired)
        self.assert_no_measurement()

    def test_raw_navigation_network_failure_does_not_become_permanent_auth_error(self):
        self.page.goto.side_effect=OSError('temporary fixture connection failure')
        with self.assertRaises(CredentialError) as error:self.execute(documents=False,summary_only=True)
        self.assertNotIsInstance(error.exception,AuthenticationRequired)
        self.assert_no_measurement()

    def test_pdf_only_interactive_keeps_manual_navigation_window(self):
        result=self.execute(capture_assets=False)
        self.assertTrue(result['pdf_collected'])
        self.assertEqual(self.mocks['auto_login.open_electronic_post'].call_args.kwargs['manual_seconds'],180)
        self.assertEqual(self.mocks['auto_login.collect'].call_args.kwargs['expected_owner'],OWNER)
        self.assert_no_measurement()

    def test_pdf_only_headless_has_no_manual_navigation_window(self):
        self.execute(capture_assets=False,headless=True)
        self.assertEqual(self.mocks['auto_login.open_electronic_post'].call_args.kwargs['manual_seconds'],0)
        self.pw.chromium.launch.assert_called_once_with(channel='msedge',headless=True)
        self.assert_no_measurement()

    def test_combined_pdf_and_assets_has_no_manual_navigation_window(self):
        self.execute()
        self.assertEqual(self.mocks['auto_login.open_electronic_post'].call_args.kwargs['manual_seconds'],0)
        self.mocks['sbi_views.capture_account'].assert_called_once()
        self.mocks['monthly_summary.capture_summary'].assert_not_called()


class TypedOwnerAvailability(unittest.TestCase):
    def test_missing_owner_raises_specific_unavailable_type(self):
        context=Mock();context.pages=[]
        with patch('auto_login.time.monotonic',side_effect=[0,1]):
            with self.assertRaises(IdentityUnavailable):authenticated_page(context,OWNER,timeout=.5)

    def test_visible_mismatch_is_not_owner_unavailable(self):
        context=Mock();page=Mock();page.url=ENTRY;page.frames=[page]
        page.is_closed.return_value=False;context.pages=[page]
        page.locator.return_value.inner_text.return_value='Wrong Owner 様 ログアウト'
        with patch('post_identity.state',return_value='MISMATCH'):
            with self.assertRaises(AuthenticationRequired) as error:authenticated_page(context,OWNER)
        self.assertNotIsInstance(error.exception,IdentityUnavailable)
        self.assertEqual(error.exception.code,'MANUAL_AUTH_REQUIRED');self.assertFalse(error.exception.retryable)

    def test_cancellation_is_not_owner_unavailable(self):
        with self.assertRaises(CredentialError) as error:authenticated_page(Mock(),OWNER,cancelled=lambda:True)
        self.assertNotIsInstance(error.exception,IdentityUnavailable)
        self.assertNotIsInstance(error.exception,AuthenticationRequired)


class SavedAuthenticationStates(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)

    def saved(self,raw):
        path=credential_path(self.base,'C');path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
        return path

    def test_missing_optional_credentials_still_return_none(self):
        self.assertIsNone(load_credentials(self.base,'C'))

    def test_invalid_header_or_encoding_is_permanent_setup_error(self):
        for raw in (b'not-a-credential',_MAGIC+b'not-base64!'):
            with self.subTest(raw=raw):
                self.saved(raw)
                with self.assertRaises(AuthenticationRequired) as error:load_credentials(self.base,'C')
                self.assertEqual(error.exception.code,'MANUAL_AUTH_REQUIRED');self.assertFalse(error.exception.retryable)

    def test_undecryptable_saved_credentials_require_manual_setup(self):
        self.saved(_MAGIC+base64.b64encode(b'anonymous-encrypted-fixture'))
        with patch('login_credentials._dpapi',side_effect=CredentialError('anonymous decryption failure')):
            with self.assertRaises(AuthenticationRequired) as error:load_credentials(self.base,'C')
        self.assertNotIn('anonymous decryption failure',str(error.exception))
        self.assertFalse(error.exception.retryable)

    def test_invalid_decrypted_payload_is_permanent_setup_error(self):
        self.saved(_MAGIC+base64.b64encode(b'anonymous-encrypted-fixture'))
        for payload in (b'invalid-json',b'[]',b'{"version":1,"username":"anonymous"}',
                        b'{"version":1,"username":123,"password":"anonymous"}',
                        b'{"version":true,"username":"anonymous","password":"anonymous"}'):
            with self.subTest(payload=payload),patch('login_credentials._dpapi',return_value=payload):
                with self.assertRaises(AuthenticationRequired):load_credentials(self.base,'C')

    def test_valid_decrypted_payload_keeps_existing_autofill_contract(self):
        self.saved(_MAGIC+base64.b64encode(b'anonymous-encrypted-fixture'))
        payload=json.dumps({'version':1,'username':'anonymous-fixture','password':'anonymous-fixture'}).encode()
        with patch('login_credentials._dpapi',return_value=payload):
            self.assertEqual(load_credentials(self.base,'C'),('anonymous-fixture','anonymous-fixture'))

    def test_temporary_file_read_failure_is_not_permanent_auth_error(self):
        with patch('pathlib.Path.read_bytes',side_effect=OSError('temporary fixture lock')):
            with self.assertRaises(CredentialError) as error:load_credentials(self.base,'C')
        self.assertNotIsInstance(error.exception,AuthenticationRequired)

    def test_automatic_missing_credentials_raise_but_manual_keeps_false(self):
        from epost_collector import autofill_login
        with patch('epost_collector.load_credentials',return_value=None),redirect_stdout(io.StringIO()):
            with self.assertRaises(AuthenticationRequired):autofill_login(Mock(),Mock(),'C',automatic=True)
            self.assertFalse(autofill_login(Mock(),Mock(),'C'))

    def test_automatic_undecryptable_credentials_propagate_original_typed_error(self):
        from epost_collector import autofill_login
        error=AuthenticationRequired('設定し直してください。')
        with patch('epost_collector.load_credentials',side_effect=error),redirect_stdout(io.StringIO()):
            with self.assertRaises(AuthenticationRequired) as actual:autofill_login(Mock(),Mock(),'C',automatic=True)
            self.assertIs(actual.exception,error)
            self.assertFalse(autofill_login(Mock(),Mock(),'C'))

    def test_temporary_autofill_read_failure_keeps_retryable_unknown_state(self):
        from epost_collector import autofill_login
        with (patch('epost_collector.load_credentials',side_effect=CredentialError('一時的な読み取り失敗')),
              redirect_stdout(io.StringIO())):
            self.assertFalse(autofill_login(Mock(),Mock(),'C',automatic=True))


if __name__=='__main__':unittest.main()
