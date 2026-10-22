"""Anonymous recovery evidence tests; no SBI, Gmail, DPAPI or private fixtures."""
from datetime import datetime,timedelta,timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from acquisition_journal import JournalError,persist_capture,replay_captures
from asset_snapshots import init as init_assets
from ledger import Store
from sbi_views import init_observations,latest
from update_workflow import fingerprint


JST=timezone(timedelta(hours=9))
START=datetime(2026,10,22,10,tzinfo=JST)
FRESH=START+timedelta(minutes=5)
OWNER='Anonymous C'
DAY='2026-09-30'


def store_at(directory):
    store=Store(directory);store.set_owner('C',OWNER)
    init_observations(store);init_assets(store)
    return store


def observation(store,stamp=FRESH,values=None,kind='口座サマリー',account='C',extras=None):
    text=stamp.isoformat() if isinstance(stamp,datetime) else stamp
    payload={'values':values or {'cash_total':123},'observed_at':text,
        'displayed_at':'2026/10/22 10:05','source':kind}
    payload.update(extras or {})
    with store.db:store.db.execute('INSERT INTO account_observations VALUES (?,?,?,?)',
        (account,kind,text,json.dumps(payload)))


def snapshot(store,values=None,as_of=DAY,stamp=FRESH,source='SBI 指定日資産',exact=True,extras=None):
    values=values or {'cash':123}
    payload={key:values.get(key) for key in ('domestic','foreign','cash','foreign_cash','growth_remaining','tsumitate_remaining')}
    payload.update(source=source,recorded_at=stamp.isoformat(),note='Anonymous source note')
    if exact:payload['evidence']={'method':'displayed-date','as_of':as_of,
        'fields':list(values),'observed_at':stamp.isoformat()}
    payload.update(extras or {})
    with store.db:store.db.execute('INSERT OR REPLACE INTO asset_snapshots VALUES (?,?,?)',
        ('C',as_of,json.dumps(payload)))
    return payload


def payload(store):
    row=store.db.execute('SELECT payload FROM asset_snapshots WHERE account=? AND as_of=?',('C',DAY)).fetchone()
    return json.loads(row['payload']) if row else None


def resign(data):
    clean={k:v for k,v in data.items() if k!='sha256'}
    data['sha256']=hashlib.sha256(json.dumps(clean,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return data


class CaptureJournal(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name)
        self.source=store_at(self.base/'source');self.stage=store_at(self.base/'stage')

    def tearDown(self):self.source.close();self.stage.close();self.tmp.cleanup()

    def persist(self):return persist_capture(self.base,'C',OWNER,self.source,START,target_dates=(DAY,'2026-10-01'))

    def test_capture_survives_discarded_temporary_ledger_and_replay_is_idempotent(self):
        with tempfile.TemporaryDirectory(dir=self.base) as temporary:
            captured=store_at(temporary);observation(captured);snapshot(captured)
            path=persist_capture(self.base,'C',OWNER,captured,START,target_dates=(DAY,))
            captured.close()
        self.assertTrue(path.is_file())
        result=replay_captures(self.base,self.stage)
        self.assertEqual((result['observations'],result['snapshots']),(1,1))
        self.assertEqual(result['accounts'],['C'])
        self.assertEqual(payload(self.stage)['cash'],123)
        before=fingerprint(self.stage.db)
        again=replay_captures(self.base,self.stage)
        self.assertEqual((again['observations'],again['snapshots']),(0,0))
        self.assertEqual(before,fingerprint(self.stage.db))

    def test_only_fresh_selected_account_values_are_persisted_no_private_content(self):
        observation(self.source,extras={'raw_screen':'raw-screen-secret','credentials':{'token':'secret-token'}},
            values={'cash_total':123,'password':'secret-password'})
        observation(self.source,stamp=START-timedelta(minutes=1),kind='My資産',values={'domestic':1})
        observation(self.source,account='A',values={'cash_total':777})
        snapshot(self.source,extras={'note':'secret-note','raw_screen':'secret-screen'})
        snapshot(self.source,as_of='2026-10-01',source='手動確認',extras={'note':'secret-manual'})
        private=self.source.directory/'private';private.mkdir();(private/'login_C.dat').write_bytes(b'secret-encrypted-file')
        path=self.persist();raw=path.read_text();data=json.loads(raw)
        self.assertEqual(len(data['observations']),1)
        self.assertEqual(len(data['snapshots']),1)
        self.assertEqual(data['target_dates'],[DAY,'2026-10-01'])
        for marker in ('secret-token','secret-password','raw-screen-secret','secret-note','secret-screen','secret-manual','secret-encrypted-file'):
            self.assertNotIn(marker,raw)
        self.assertEqual((private/'login_C.dat').read_bytes(),b'secret-encrypted-file')

    def test_timezone_comparison_selects_real_freshness_not_iso_lexical_order(self):
        observation(self.source,stamp=FRESH.astimezone(timezone.utc))
        observation(self.source,stamp=START-timedelta(minutes=1),kind='My資産',values={'domestic':2})
        snapshot(self.source,stamp=FRESH.astimezone(timezone.utc))
        data=json.loads(self.persist().read_text())
        self.assertEqual(len(data['observations']),1)
        self.assertTrue(data['observations'][0]['observed_at'].endswith('+09:00'))
        result=replay_captures(self.base,self.stage)
        self.assertEqual((result['observations'],result['snapshots']),(1,1))

    def test_empty_or_stale_capture_creates_no_journal(self):
        observation(self.source,stamp=START-timedelta(seconds=1))
        snapshot(self.source,stamp=START-timedelta(seconds=1))
        self.assertIsNone(self.persist())
        self.assertFalse((self.base/'data/acquisitions').exists())

    def test_all_fresh_intraday_observations_and_validated_capture_window_survive(self):
        observation(self.source,stamp=FRESH,values={'cash_total':123})
        observation(self.source,stamp=FRESH+timedelta(minutes=1),values={'cash_total':456})
        path=self.persist();data=json.loads(path.read_text())
        self.assertEqual([row['payload']['values']['cash_total'] for row in data['observations']],[123,456])
        result=replay_captures(self.base,self.stage)
        self.assertEqual(result['observations'],2)
        self.assertEqual(result['windows'],[{'account':'C','since':START.isoformat(),'acquired_at':data['acquired_at']}])
        again=replay_captures(self.base,self.stage)
        self.assertEqual(again['observations'],0)
        self.assertEqual(again['windows'],result['windows'])

    def test_naive_since_is_rejected_and_database_is_untouched(self):
        observation(self.source);before=fingerprint(self.source.db)
        with self.assertRaises(JournalError):persist_capture(self.base,'C',OWNER,self.source,START.replace(tzinfo=None))
        self.assertEqual(before,fingerprint(self.source.db))

    def test_persist_rejects_wrong_owner(self):
        observation(self.source)
        with self.assertRaisesRegex(JournalError,'名義'):
            persist_capture(self.base,'C','Other identity',self.source,START)
        self.assertFalse((self.base/'data/acquisitions').exists())

    def test_atomic_file_failure_leaves_existing_journals_unchanged(self):
        observation(self.source);path=self.persist();before=path.read_bytes()
        with patch('acquisition_journal.os.replace',side_effect=OSError('simulated disk failure')):
            with self.assertRaises(OSError):self.persist()
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(list(path.parent.iterdir()),[path])

    def test_checksum_failure_blocks_before_any_valid_journal_is_applied(self):
        observation(self.source);self.persist();second=self.persist()
        data=json.loads(second.read_text());data['observations'][0]['payload']['values']['cash_total']=999
        second.write_text(json.dumps(data))
        before=fingerprint(self.stage.db)
        with self.assertRaisesRegex(JournalError,'チェックサム'):replay_captures(self.base,self.stage)
        self.assertEqual(before,fingerprint(self.stage.db))

    def test_changed_account_owner_refuses_replay(self):
        observation(self.source);self.persist();self.stage.set_owner('C','Changed owner')
        before=fingerprint(self.stage.db)
        with self.assertRaisesRegex(JournalError,'名義'):replay_captures(self.base,self.stage)
        self.assertEqual(before,fingerprint(self.stage.db))

    def test_valid_checksum_does_not_make_unknown_secret_schema_acceptable(self):
        observation(self.source);path=self.persist();data=json.loads(path.read_text())
        data['observations'][0]['payload']['values']['refresh_token']='do-not-execute-or-copy'
        path.write_text(json.dumps(resign(data)))
        before=fingerprint(self.stage.db)
        with self.assertRaises(JournalError):replay_captures(self.base,self.stage)
        self.assertEqual(before,fingerprint(self.stage.db))

    def test_invalid_exact_evidence_even_with_valid_checksum_is_rejected(self):
        snapshot(self.source);path=self.persist();data=json.loads(path.read_text())
        data['snapshots'][0]['payload']['evidence']['as_of']='2026-10-01'
        path.write_text(json.dumps(resign(data)))
        with self.assertRaises(JournalError):replay_captures(self.base,self.stage)

    def test_manual_dated_snapshot_is_preserved_including_its_blanks(self):
        snapshot(self.source,values={'cash':999,'domestic':222})
        original=snapshot(self.stage,values={'cash':50},source='手動確認',exact=False)
        self.persist();result=replay_captures(self.base,self.stage)
        self.assertEqual(result['snapshots'],0)
        self.assertEqual(payload(self.stage),original)
        self.assertIn('MANUAL_ASSET_PRESERVED:C:'+DAY,result['warnings'])

    def test_newer_derived_value_never_replaces_exact_displayed_date(self):
        snapshot(self.source,values={'cash':999},stamp=FRESH+timedelta(minutes=10),source='SBI取得当日',exact=False)
        original=snapshot(self.stage,values={'cash':50},stamp=START-timedelta(days=1))
        self.persist();result=replay_captures(self.base,self.stage)
        self.assertEqual(result['snapshots'],0);self.assertEqual(payload(self.stage),original)

    def test_older_exact_value_replaces_newer_derived_snapshot(self):
        snapshot(self.source,values={'cash':123})
        snapshot(self.stage,values={'cash':999},stamp=FRESH+timedelta(minutes=10),source='SBI My資産の前月比',exact=False)
        self.persist();result=replay_captures(self.base,self.stage)
        self.assertEqual(result['snapshots'],1)
        self.assertEqual(payload(self.stage)['cash'],123)
        self.assertEqual(payload(self.stage)['evidence']['fields'],['cash'])

    def test_complementary_exact_fields_merge_without_inventing_missing_values(self):
        snapshot(self.source,values={'domestic':222})
        snapshot(self.stage,values={'cash':50})
        self.persist();result=replay_captures(self.base,self.stage)
        self.assertEqual(result['snapshots'],1)
        merged=payload(self.stage)
        self.assertEqual((merged['cash'],merged['domestic']),(50,222))
        self.assertIsNone(merged['foreign'])
        self.assertEqual(merged['evidence']['fields'],['cash','domestic'])
        self.assertEqual(replay_captures(self.base,self.stage)['snapshots'],0)

    def test_exact_conflict_rolls_back_observations_inserted_earlier_in_replay(self):
        observation(self.source);snapshot(self.source,values={'cash':123})
        snapshot(self.stage,values={'cash':50});self.persist()
        before=fingerprint(self.stage.db)
        with self.assertRaisesRegex(JournalError,'確認済み資産'):replay_captures(self.base,self.stage)
        self.assertEqual(before,fingerprint(self.stage.db))

    def test_newer_existing_observation_remains_latest_after_older_replay(self):
        observation(self.source,values={'cash_total':123})
        observation(self.stage,stamp=FRESH+timedelta(minutes=1),values={'cash_total':999})
        self.persist();result=replay_captures(self.base,self.stage)
        self.assertEqual(result['observations'],1)
        self.assertEqual(latest(self.stage,'2026-10-22')['C','口座サマリー']['values']['cash_total'],999)

    def test_equivalent_timezone_timestamp_does_not_duplicate_existing_observation(self):
        observation(self.source)
        observation(self.stage,stamp=FRESH.astimezone(timezone.utc))
        self.persist();result=replay_captures(self.base,self.stage)
        self.assertEqual(result['observations'],0)
        self.assertEqual(self.stage.db.execute('SELECT count(*) FROM account_observations').fetchone()[0],1)


if __name__=='__main__':unittest.main()
