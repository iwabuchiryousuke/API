"""Synthetic authentication-hold records; no account authentication is run."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gmail_auth import GmailError
from ledger import LedgerError
from login_credentials import CredentialError
from monthly_schedule import due_slot
import monthly_attention as ma


OWNER='Anonymous C'
END=due_slot(datetime.fromisoformat('2026-10-31T16:00:00+09:00'))
START=due_slot(datetime.fromisoformat('2026-11-01T16:00:00+09:00'))


def resign(value):
    raw={k:v for k,v in value.items() if k!='sha256'}
    value['sha256']=hashlib.sha256(json.dumps(raw,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    return value


class AttentionClassification(unittest.TestCase):
    def test_explicit_nonretryable_credential_error_needs_manual_attention(self):
        class AuthenticationRequired(CredentialError):retryable=False
        self.assertTrue(ma.attention_required(AuthenticationRequired('synthetic manual condition')))

    def test_all_positive_manual_gmail_codes_are_recognized(self):
        for code in ma.MANUAL_CODES:
            with self.subTest(code=code):self.assertTrue(ma.attention_required(GmailError(code,'synthetic reason')))

    def test_transient_network_timeout_and_api_limit_do_not_block_future_retries(self):
        for error in [TimeoutError('network timeout'),OSError('network down'),
                      GmailError('CONNECTION','network'),GmailError('CONSENT_TIMEOUT','timeout'),
                      GmailError('API_LIMIT','limited'),GmailError('UNKNOWN','unknown')]:
            with self.subTest(error=type(error).__name__):self.assertFalse(ma.attention_required(error))

    def test_generic_error_message_is_not_parsed_as_authentication_status(self):
        self.assertFalse(ma.attention_required(CredentialError('[NEEDS_CONSENT] untyped message')))
        self.assertFalse(ma.attention_required(ValueError('MANUAL_AUTH_REQUIRED')))
        self.assertFalse(ma.attention_required(GmailError(['NEEDS_CONSENT'],'malformed typed code')))


class AttentionFiles(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name)
        private=self.base/'data/private';private.mkdir(parents=True)
        self.secret=private/'login_C.dat';self.secret.write_bytes(b'opaque encrypted synthetic bytes')

    def save(self,slot=END,account='C',owner=OWNER,code='MANUAL_AUTH_REQUIRED'):
        return ma.save_attention(self.base,slot,account,owner,code)

    def test_absent_hold_check_is_read_only(self):
        self.assertFalse(ma.blocked(self.base,END,'C',OWNER))
        self.assertFalse((self.base/'data/scheduler').exists())

    def test_saved_hold_is_bound_to_owner_account_slot_and_checksum(self):
        path=self.save()
        self.assertEqual(path,self.base/'data/scheduler/attention/2026-10-31/C.json')
        self.assertTrue(ma.blocked(self.base,END,'C',OWNER))
        self.assertFalse(ma.blocked(self.base,START,'C',OWNER))
        self.assertFalse(ma.blocked(self.base,END,'A','Anonymous A'))
        value=json.loads(path.read_text());self.assertEqual(value['code'],'MANUAL_AUTH_REQUIRED')
        self.assertEqual(value['slot'],END);self.assertEqual(value['sha256'],resign(value)['sha256'])
        self.assertEqual(self.secret.read_bytes(),b'opaque encrypted synthetic bytes')

    def test_saving_existing_hold_is_idempotent_without_rewriting(self):
        path=self.save();before=path.read_bytes()
        self.assertEqual(self.save(code='NEEDS_CONSENT'),path)
        self.assertEqual(path.read_bytes(),before)

    def test_owner_normalization_matches_financial_record_binding(self):
        self.save(owner='Ａｎｏｎｙｍｏｕｓ　Ｃ\u200b')
        self.assertTrue(ma.blocked(self.base,END,'C','Anonymous C'))

    def test_different_owner_raises_and_marker_is_retained(self):
        path=self.save();before=path.read_bytes()
        with self.assertRaises(LedgerError):ma.blocked(self.base,END,'C','Another Person')
        with self.assertRaises(LedgerError):ma.clear_attention(self.base,'C','Another Person')
        self.assertEqual(path.read_bytes(),before)

    def test_changed_payload_without_checksum_is_corrupt(self):
        path=self.save();value=json.loads(path.read_text());value['owner']='Other';path.write_text(json.dumps(value))
        with self.assertRaises(LedgerError):ma.blocked(self.base,END,'C',OWNER)
        self.assertTrue(path.exists())

    def test_resigned_wrong_date_or_role_is_still_rejected(self):
        for change in ['date','role']:
            with self.subTest(change=change):
                path=self.save();original=path.read_bytes();value=json.loads(original)
                if change=='date':value['slot']=START
                else:value['slot']['role']='month-start'
                path.write_text(json.dumps(resign(value)))
                with self.assertRaises(LedgerError):ma.blocked(self.base,END,'C',OWNER)
                path.write_bytes(original)

    def test_raw_error_or_credential_like_reason_is_never_accepted(self):
        raw='https://fixture.invalid/?token=do-not-store'
        with self.assertRaises(LedgerError):self.save(code=raw)
        self.assertFalse((self.base/'data/scheduler').exists())

    def test_duplicate_json_keys_and_unknown_fields_are_rejected(self):
        path=self.save();original=path.read_bytes()
        path.write_text(original.decode().replace('"account":"C"','"account":"C","account":"C"',1))
        with self.assertRaises(LedgerError):ma.blocked(self.base,END,'C',OWNER)
        value=json.loads(original);value['raw_error']='private secret';path.write_text(json.dumps(resign(value)))
        with self.assertRaises(LedgerError) as error:ma.blocked(self.base,END,'C',OWNER)
        self.assertNotIn('private secret',str(error.exception))

    def test_clear_validates_all_before_deleting_any_marker(self):
        end=self.save();start=self.save(START);start.write_bytes(b'corrupt')
        with self.assertRaises(LedgerError):ma.clear_attention(self.base,'C',OWNER)
        self.assertTrue(end.exists());self.assertTrue(start.exists())

    def test_explicit_successful_test_clear_removes_only_bound_account(self):
        end=self.save();start=self.save(START);other=self.save(account='A',owner='Anonymous A')
        note=self.base/'data/scheduler/attention/user_notes.txt';note.write_text('keep note')
        self.assertEqual(ma.clear_attention(self.base,'C',OWNER),2)
        self.assertFalse(end.exists());self.assertFalse(start.exists());self.assertTrue(other.exists())
        self.assertEqual(note.read_text(),'keep note')
        self.assertEqual(self.secret.read_bytes(),b'opaque encrypted synthetic bytes')
        self.assertEqual(ma.clear_attention(self.base,'C',OWNER),0)

    def test_clear_when_absent_does_not_create_scheduler_tree(self):
        self.assertEqual(ma.clear_attention(self.base,'C',OWNER),0)
        self.assertFalse((self.base/'data/scheduler').exists())

    def test_invalid_account_owner_and_slot_fail_before_mutation(self):
        for account,owner,slot in [('E',OWNER,END),('C','',END),('C',OWNER,dict(END,slot_date='../outside'))]:
            with self.subTest(account=account,owner=owner):
                with self.assertRaises(LedgerError):ma.save_attention(self.base,slot,account,owner)
        self.assertFalse((self.base/'data/scheduler').exists())

    def test_atomic_write_failure_does_not_create_valid_marker_or_change_credentials(self):
        with patch.object(ma.os,'replace',side_effect=OSError('injected failure')):
            with self.assertRaises(LedgerError):self.save()
        self.assertFalse(ma.blocked(self.base,END,'C',OWNER))
        self.assertEqual(list((self.base/'data/scheduler/attention/2026-10-31').glob('*.tmp')),[])
        self.assertEqual(self.secret.read_bytes(),b'opaque encrypted synthetic bytes')

    def test_symlinked_attention_directory_and_target_are_rejected(self):
        outside=self.base/'outside';outside.mkdir()
        scheduler=self.base/'data/scheduler';scheduler.mkdir()
        try:(scheduler/'attention').symlink_to(outside,target_is_directory=True)
        except OSError:self.skipTest('symlink creation unavailable')
        with self.assertRaises(LedgerError):self.save()
        self.assertEqual(list(outside.iterdir()),[])
        (scheduler/'attention').unlink();path=self.save();path.unlink();target=outside/'sensitive.json';target.write_text('sensitive synthetic bytes')
        path.symlink_to(target)
        with self.assertRaises(LedgerError):ma.blocked(self.base,END,'C',OWNER)
        with self.assertRaises(LedgerError):ma.clear_attention(self.base,'C',OWNER)
        self.assertEqual(target.read_text(),'sensitive synthetic bytes')

    def test_hardlinked_marker_is_rejected_without_modifying_other_copy(self):
        path=self.save();linked=self.base/'outside_link.json'
        try:linked.hardlink_to(path)
        except OSError:self.skipTest('hardlink creation unavailable')
        before=linked.read_bytes()
        with self.assertRaises(LedgerError):ma.blocked(self.base,END,'C',OWNER)
        self.assertEqual(linked.read_bytes(),before)


if __name__=='__main__':unittest.main()
