"""Read-only capture previews use anonymous disposable ledgers only."""
from datetime import datetime,timedelta,timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from acquisition_journal import JournalError,persist_capture,replay_captures
from asset_preview import preview_assets
from ledger import Store
from update_workflow import Workflow,UpdateError


JST=timezone(timedelta(hours=9))
DAY='2026-10-22'
START=datetime(2026,10,22,10,tzinfo=JST)
FRESH=START+timedelta(minutes=5)
OWNER='Anonymous C'


def observations_table(store):
    store.db.execute('CREATE TABLE IF NOT EXISTS account_observations('
        'account TEXT,kind TEXT,observed_at TEXT,payload TEXT,PRIMARY KEY(account,kind,observed_at))')
    store.db.commit()


def snapshot_table(store):
    store.db.execute('CREATE TABLE IF NOT EXISTS asset_snapshots('
        'account TEXT NOT NULL REFERENCES accounts(id),as_of TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(account,as_of))')
    store.db.commit()


def observation(store,stamp=FRESH,amount=123,kind='口座サマリー'):
    observations_table(store)
    raw=stamp.isoformat() if isinstance(stamp,datetime) else stamp
    values={'cash_total':amount} if kind=='口座サマリー' else {'domestic':amount,'fund':0,'bond':0}
    payload={'observed_at':raw,'displayed_at':None,'source':kind,'values':values}
    with store.db:store.db.execute('INSERT INTO account_observations VALUES (?,?,?,?)',
        ('C',kind,raw,json.dumps(payload)))


def snapshot(store,stamp=FRESH,as_of='2026-09-30',amount=456,exact=False):
    snapshot_table(store)
    payload=dict(domestic=None,foreign=None,cash=amount,foreign_cash=None,
        growth_remaining=None,tsumitate_remaining=None,source='SBI取得当日',recorded_at=stamp.isoformat())
    if exact:
        payload['source']='SBI 指定日資産'
        payload['evidence']={'method':'displayed-date','as_of':as_of,
                             'fields':['cash'],'observed_at':stamp.isoformat()}
    with store.db:store.db.execute('INSERT OR REPLACE INTO asset_snapshots VALUES (?,?,?)',
        ('C',as_of,json.dumps(payload)))


class AssetPreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name)
        self.live=Store(self.base/'data');self.live.set_owner('C',OWNER)
        self.source=Store(self.base/'source');self.source.set_owner('C',OWNER)

    def tearDown(self):self.source.close();self.live.close();self.tmp.cleanup()

    def persist(self,since=START):
        return persist_capture(self.base,'C',OWNER,self.source,since)

    def fingerprint(self):
        return hashlib.sha256((self.base/'data/ledger.sqlite3').read_bytes()).hexdigest()

    def test_pending_values_survive_failed_pdf_and_preview_does_not_commit(self):
        observation(self.source);snapshot(self.source,exact=True);self.persist()
        folder=self.base/'data/電子ポストPDF/C';folder.mkdir(parents=True)
        (folder/'unknown.pdf').write_bytes(b'not-a-pdf')
        with self.assertRaises(UpdateError):Workflow(self.base).run(review=lambda *args:True)
        before=self.fingerprint()
        result=preview_assets(self.base,self.live,DAY)
        self.assertEqual(result['observations']['C','口座サマリー']['values']['cash_total'],123)
        self.assertEqual(result['snapshots']['C','2026-09-30']['cash'],456)
        self.assertEqual(result['committed_observations'],{})
        self.assertEqual(result['pending_accounts'],['C'])
        self.assertEqual(len(result['windows']),1)
        self.assertEqual(self.live.db.execute('SELECT count(*) FROM account_observations').fetchone()[0],0)
        self.assertEqual(self.fingerprint(),before)

    def test_repeated_preview_keeps_database_and_journals_byte_identical(self):
        observation(self.source);path=self.persist()
        before=self.fingerprint();raw=path.read_bytes()
        first=preview_assets(self.base,self.live,DAY)
        again=preview_assets(self.base,self.live,DAY)
        self.assertEqual(first,again)
        self.assertEqual(self.fingerprint(),before)
        self.assertEqual(path.read_bytes(),raw)
        self.assertFalse((self.base/'data/backups').exists())

    def test_empty_preview_does_not_initialize_live_asset_schemas(self):
        before=self.fingerprint()
        result=preview_assets(self.base,self.live,DAY)
        self.assertEqual(result['observations'],{})
        names={row[0] for row in self.live.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn('asset_snapshots',names)
        self.assertNotIn('account_observations',names)
        self.assertEqual(self.fingerprint(),before)

    def test_corrupt_journal_falls_back_to_committed_values_with_warning(self):
        observation(self.live,stamp=START-timedelta(minutes=1),amount=50)
        observation(self.source);path=self.persist();path.write_bytes(b'{broken')
        before=self.fingerprint()
        result=preview_assets(self.base,self.live,DAY)
        self.assertEqual(result['observations']['C','口座サマリー']['values']['cash_total'],50)
        self.assertEqual(result['observations'],result['committed_observations'])
        self.assertEqual(result['pending_accounts'],[])
        self.assertEqual(result['windows'],[])
        self.assertTrue(any('登録済みの資産だけ' in value for value in result['warnings']))
        self.assertEqual(self.fingerprint(),before)

    def test_owner_mismatch_falls_back_without_exposing_mismatched_values(self):
        observation(self.source);self.persist();self.live.set_owner('C','Changed identity')
        before=self.fingerprint()
        result=preview_assets(self.base,self.live,DAY)
        self.assertEqual(result['observations'],{})
        self.assertEqual(result['pending_accounts'],[])
        self.assertEqual(result['windows'],[])
        self.assertTrue(result['warnings'])
        self.assertEqual(self.fingerprint(),before)

    def test_partial_mutating_replay_error_never_leaks_partial_values(self):
        observation(self.live,stamp=START-timedelta(minutes=1),amount=50)
        before=self.fingerprint()
        def broken_replay(base,stage):
            observation(stage,stamp=FRESH,amount=999)
            raise JournalError('simulated failure after mutation')
        with patch('asset_preview.replay_captures',side_effect=broken_replay):
            result=preview_assets(self.base,self.live,DAY,target_dates=('2026-10-01',))
        self.assertEqual(result['observations']['C','口座サマリー']['values']['cash_total'],50)
        self.assertEqual(len(result['history_observations']),1)
        self.assertEqual(result['pending_accounts'],[])
        self.assertEqual(result['windows'],[])
        self.assertEqual(self.fingerprint(),before)

    def test_older_journal_and_duplicate_journal_do_not_claim_pending_current_values(self):
        observation(self.live,stamp=FRESH+timedelta(minutes=1),amount=999)
        snapshot(self.live,stamp=FRESH+timedelta(minutes=1),amount=999)
        observation(self.source);snapshot(self.source);self.persist()
        result=preview_assets(self.base,self.live,DAY)
        self.assertEqual(result['pending_accounts'],[])
        self.assertEqual(result['observations']['C','口座サマリー']['values']['cash_total'],999)
        self.assertEqual(result['snapshots']['C','2026-09-30']['cash'],999)
        replay_captures(self.base,self.live)
        result=preview_assets(self.base,self.live,DAY)
        self.assertEqual(result['pending_accounts'],[])

    def test_cutoff_uses_actual_jst_date_and_latest_actual_time(self):
        # UTC representation sorts incorrectly against JST strings lexically.
        observation(self.live,stamp=datetime(2026,9,30,23,tzinfo=JST),amount=10)
        observation(self.live,stamp=datetime(2026,9,30,16,tzinfo=timezone.utc),amount=20)
        observation(self.live,stamp=datetime(2026,10,1,1,30,tzinfo=JST),amount=30)
        snapshot(self.live,as_of='2026-10-01')
        september=preview_assets(self.base,self.live,'2026-09-30')
        self.assertEqual(september['observations']['C','口座サマリー']['values']['cash_total'],10)
        self.assertEqual(len(september['history_observations']),1)
        self.assertEqual(september['snapshots'],{})
        october=preview_assets(self.base,self.live,'2026-10-01')
        self.assertEqual(october['observations']['C','口座サマリー']['values']['cash_total'],30)
        self.assertEqual(len(october['history_observations']),3)

    def test_naive_or_invalid_existing_records_warn_without_crashing(self):
        observation(self.live,stamp='2026-10-22T10:00:00',amount=1)
        observation(self.live,stamp=FRESH,amount=123)
        snapshot_table(self.live)
        with self.live.db:self.live.db.execute('INSERT INTO asset_snapshots VALUES (?,?,?)',('C','not-a-date','{}'))
        before=self.fingerprint()
        result=preview_assets(self.base,self.live,DAY)
        self.assertEqual(len(result['history_observations']),1)
        self.assertEqual(result['observations']['C','口座サマリー']['values']['cash_total'],123)
        self.assertTrue(any('取得時刻' in value for value in result['warnings']))
        self.assertEqual(self.fingerprint(),before)

    def test_history_retains_earliest_and_latest_rows_and_validated_windows(self):
        early=datetime(2026,10,1,9,tzinfo=JST);late=early+timedelta(hours=6)
        observation(self.source,stamp=early,amount=100,kind='My資産')
        observation(self.source,stamp=late,amount=200,kind='My資産')
        self.persist(since=early-timedelta(minutes=1))
        result=preview_assets(self.base,self.live,DAY,target_dates=('2026-09-30','2026-10-01'))
        self.assertEqual([row['observed_at'] for row in result['history_observations']],[early.isoformat(),late.isoformat()])
        self.assertEqual(result['windows'][0]['since'],(early-timedelta(minutes=1)).isoformat())
        candidate=result['boundary_candidates']['C']['2026-10-01']
        self.assertEqual(candidate['values']['domestic'],100)
        self.assertEqual(candidate['observed_from'],early.isoformat())
        self.assertFalse(candidate['exact_boundary'])

    def test_future_dates_and_future_values_are_not_filled_in_preview(self):
        observation(self.source,stamp=datetime(2026,11,1,9,tzinfo=JST))
        snapshot(self.source,as_of='2026-11-01',stamp=datetime(2026,11,1,9,tzinfo=JST))
        self.persist()
        result=preview_assets(self.base,self.live,DAY,target_dates=('2026-11-01',))
        self.assertEqual(result['observations'],{})
        self.assertEqual(result['snapshots'],{})
        self.assertEqual(result['pending_accounts'],[])
        self.assertEqual(result['boundary_candidates'],{})
        self.assertTrue(any('表示基準日より後' in value for value in result['warnings']))

    def test_io_error_during_replay_falls_back_without_database_mutation(self):
        before=self.fingerprint()
        with patch('asset_preview.replay_captures',side_effect=OSError('simulated IO error')):
            result=preview_assets(self.base,self.live,DAY)
        self.assertEqual(result['pending_accounts'],[])
        self.assertEqual(result['windows'],[])
        self.assertTrue(result['warnings'])
        self.assertEqual(self.fingerprint(),before)

    def test_invalid_cutoff_is_rejected_before_any_database_change(self):
        before=self.fingerprint()
        with self.assertRaises(ValueError):preview_assets(self.base,self.live,'not-a-date')
        self.assertEqual(self.fingerprint(),before)

    def test_uncommitted_live_transaction_is_not_presented_as_registered(self):
        observation(self.live,amount=50)
        self.live.db.execute('UPDATE account_observations SET payload=?',
            (json.dumps({'values':{'cash_total':999},'observed_at':FRESH.isoformat(),'source':'口座サマリー'}),))
        result=preview_assets(self.base,self.live,DAY)
        self.assertEqual(result['committed_observations'],{})
        self.assertTrue(any('未確定の台帳操作' in value for value in result['warnings']))
        self.assertTrue(self.live.db.in_transaction)
        self.live.db.rollback()

    def test_invalid_existing_amounts_warn_instead_of_reaching_number_formatter(self):
        observation(self.live,amount='not-a-number')
        observation(self.live,kind='My資産',amount=123)
        before=self.fingerprint()
        result=preview_assets(self.base,self.live,DAY)
        self.assertNotIn(('C','口座サマリー'),result['observations'])
        self.assertIn(('C','My資産'),result['observations'])
        self.assertTrue(result['warnings'])
        self.assertEqual(self.fingerprint(),before)

    def test_same_instant_conflicting_live_values_are_not_arbitrarily_selected(self):
        observation(self.live,stamp=FRESH,amount=123)
        observation(self.live,stamp=FRESH.astimezone(timezone.utc),amount=999)
        result=preview_assets(self.base,self.live,DAY)
        self.assertNotIn(('C','口座サマリー'),result['observations'])
        self.assertEqual(len(result['history_observations']),2)
        self.assertTrue(any('同じ取得時刻' in value for value in result['warnings']))

    def test_manual_snapshot_is_preserved_and_explained_in_japanese(self):
        snapshot(self.live,amount=50)
        row=self.live.db.execute('SELECT payload FROM asset_snapshots').fetchone()
        payload=json.loads(row['payload']);payload['source']='手動確認'
        with self.live.db:self.live.db.execute('UPDATE asset_snapshots SET payload=?',(json.dumps(payload),))
        snapshot(self.source,amount=123,exact=True);self.persist()
        before=self.fingerprint()
        result=preview_assets(self.base,self.live,DAY)
        self.assertEqual(result['snapshots']['C','2026-09-30']['cash'],50)
        self.assertEqual(result['pending_accounts'],[])
        self.assertTrue(any('手動資産記録を保持' in value for value in result['warnings']))
        self.assertFalse(any(value.startswith('MANUAL_ASSET_PRESERVED:') for value in result['warnings']))
        self.assertEqual(self.fingerprint(),before)


if __name__=='__main__':unittest.main()
