"""Anonymous regressions for complete, atomic multi-item PDF notice storage.

The inputs below are generated dictionaries, never uploaded financial records.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

import activity
from activity import Activities, key
from ledger import LedgerError, Store


def canonical_hash(value):
    encoded=json.dumps(value,ensure_ascii=False,sort_keys=True,
                       separators=(',',':'),allow_nan=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def dividend(code='9900',quantity='7',price='5',name='Anonymous Security A'):
    amount=str(int(quantity)*int(price))
    return dict(type='配当',date='2026-06-25',name=name,code=code,
                quantity=quantity,per_share=price,gross=amount,tax='0',net=amount,
                custody='NISA',currency='JPY')


def document(events,*,owner='Example A',with_items=True,raw_suffix=b''):
    events=deepcopy(list(events))
    raw=json.dumps(events,ensure_ascii=False,sort_keys=True).encode()+raw_suffix
    result=SimpleNamespace(owner=owner,item=events[0],raw=raw,
                           digest=hashlib.sha256(raw).hexdigest(),
                           filename='anonymous-notice.pdf')
    if with_items:result.items=tuple(events)
    return result


class CompleteNoticeStorage(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'data')
        self.addCleanup(self.store.close)
        self.store.set_owner('A','Example A')
        self.store.set_owner('B','Example B')
        self.actions=Activities(self.store)
        self.first=dividend()
        self.second=dividend('9901','10','3','Anonymous Security B')

    def tables(self):
        return {row[0] for row in self.store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}

    def dump(self):
        return '\n'.join(self.store.db.iterdump())

    def stored(self):
        return [dict(row) for row in self.store.db.execute(
            'SELECT * FROM activities ORDER BY id')]

    def seed_event(self,event,account='A',*,payload=None):
        payload=payload or json.dumps(event,ensure_ascii=False,indent=2)
        with self.store.db:
            return self.store.db.execute(
                'INSERT INTO activities(account,kind,event_key,event_date,payload,source) '
                'VALUES(?,?,?,?,?,?)',
                (account,event['type'],key(event),event['date'],payload,'legacy-first.pdf')).lastrowid

    def seed_partial(self,doc):
        ident=self.seed_event(doc.item)
        with self.store.db:
            self.store.db.execute(
                'INSERT INTO activity_files(account,digest,activity_id) VALUES(?,?,?)',
                ('A',doc.digest,ident))
        return ident

    def manifest(self,doc):
        row=self.store.db.execute(
            'SELECT * FROM activity_documents WHERE account=? AND digest=?',
            ('A',doc.digest)).fetchone()
        return dict(row),json.loads(row['manifest'])

    def assert_complete(self,doc):
        self.assertTrue(self.actions.notice_is_complete('A',doc.digest))
        self.assertIn(doc.digest,self.actions.completed_notice_digests('A'))
        self.assertFalse(self.actions.notice_is_complete('B',doc.digest))

    def test_readonly_completion_queries_and_preview_do_not_create_tables_or_write_database(self):
        doc=document([self.first,self.second])
        before=self.store.directory.joinpath('ledger.sqlite3').read_bytes()
        self.assertFalse(self.actions.notice_is_complete('A',doc.digest))
        self.assertEqual(self.actions.completed_notice_digests('A'),set())
        preview=self.actions.preview_notice('A',doc)
        self.assertFalse(preview['already'])
        self.assertFalse(preview['same_event'])
        self.assertEqual((preview['new'],preview['existing']),(2,0))
        self.assertNotIn('activity_documents',self.tables())
        self.assertNotIn('activity_file_events',self.tables())
        self.assertEqual(self.store.directory.joinpath('ledger.sqlite3').read_bytes(),before)

    def test_schema_initializer_participates_in_caller_transaction(self):
        self.store.db.execute('BEGIN IMMEDIATE')
        try:
            activity.init_notice_tables(self.store.db)
            self.assertIn('activity_documents',self.tables())
            self.assertIn('activity_file_events',self.tables())
        finally:self.store.db.rollback()
        self.assertNotIn('activity_documents',self.tables())
        self.assertNotIn('activity_file_events',self.tables())

    def test_fresh_multi_item_notice_has_one_anchor_and_verified_links_for_every_event(self):
        doc=document([self.first,self.second])
        result=self.actions.import_notice('A',doc)
        self.assertEqual((result['new'],result['existing']),(2,0))
        self.assertFalse(result['already'])
        self.assertEqual(len(self.stored()),2)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM activity_files').fetchone()[0],1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM activity_file_events').fetchone()[0],2)
        state,manifest=self.manifest(doc)
        self.assertEqual((state['parser_revision'],state['item_count']),(2,2))
        self.assertEqual({entry['event_key']:entry['payload_sha256'] for entry in manifest},
                         {key(event):canonical_hash(event) for event in (self.first,self.second)})
        self.assert_complete(doc)
        self.assertEqual(self.store.db.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_legacy_first_only_anchor_is_not_complete_and_missing_item_is_recovered(self):
        doc=document([self.first,self.second])
        first_id=self.seed_partial(doc)
        first_before=self.stored()[0]
        anchor_before=dict(self.store.db.execute('SELECT * FROM activity_files').fetchone())
        self.assertFalse(self.actions.notice_is_complete('A',doc.digest))
        preview=self.actions.preview_notice('A',doc)
        self.assertFalse(preview['already'])
        self.assertEqual((preview['new'],preview['existing']),(1,1))
        result=self.actions.import_notice('A',doc)
        self.assertEqual((result['new'],result['existing']),(1,1))
        rows=self.stored()
        first_after=next(row for row in rows if row['id']==first_id)
        for field in ('id','account','event_key','event_date','payload'):
            self.assertEqual(first_after[field],first_before[field])
        self.assertEqual(dict(self.store.db.execute('SELECT * FROM activity_files').fetchone()),anchor_before)
        self.assertEqual(len(rows),2)
        self.assert_complete(doc)
        before=self.dump()
        again=self.actions.import_notice('A',doc)
        self.assertTrue(again['already'])
        self.assertEqual((again['new'],again['existing']),(0,2))
        self.assertEqual(self.dump(),before)

    def test_old_single_item_document_without_items_or_with_empty_items_remains_supported(self):
        for empty_items in (False,True):
            with self.subTest(empty_items=empty_items):
                event=dividend('9902' if empty_items else '9903')
                doc=document([event],with_items=False)
                if empty_items:doc.items=()
                self.actions.import_notice('A',doc)
                self.assert_complete(doc)
                self.assertTrue(self.actions.import_notice('A',doc)['already'])
        self.assertEqual(len(self.stored()),2)

    def test_different_file_with_identical_events_attaches_evidence_without_double_income(self):
        first=document([self.first,self.second])
        self.actions.import_notice('A',first)
        other=document([self.first,self.second],raw_suffix=b'\nanonymous second file')
        result=self.actions.import_notice('A',other)
        self.assertEqual((result['new'],result['existing']),(0,2))
        self.assertTrue(result['same_event'])
        self.assertEqual(len(self.stored()),2)
        self.assert_complete(first)
        self.assert_complete(other)

    def test_same_security_distinct_physical_rows_are_peers_on_initial_import_and_reparse(self):
        peer=dividend('9900','8','5',self.first['name'])
        doc=document([self.first,peer])
        self.actions.import_notice('A',doc)
        self.assertEqual(len(self.stored()),2)
        self.assert_complete(doc)
        self.assertTrue(self.actions.preview_notice('A',doc)['already'])
        self.assertTrue(self.actions.import_notice('A',doc)['already'])
        self.assertEqual(len(self.stored()),2)

    def test_outside_conflicting_row_is_not_authorized_by_other_document_peers(self):
        outside=dividend('9900','9','5',self.first['name'])
        self.seed_event(outside)
        doc=document([self.first,dividend('9900','8','5',self.first['name'])])
        before=self.dump()
        with self.assertRaises(LedgerError):self.actions.import_notice('A',doc)
        self.assertEqual(self.dump(),before)
        self.assertNotIn('activity_documents',self.tables())

    def test_identical_items_are_ambiguous_and_do_not_create_partial_state(self):
        doc=document([self.first,self.first])
        before=self.dump()
        with self.assertRaises(LedgerError):self.actions.import_notice('A',doc)
        self.assertEqual(self.dump(),before)
        self.assertNotIn('activity_documents',self.tables())

    def test_later_item_conflict_is_found_before_new_first_item_is_written(self):
        conflict=dict(self.second,net='29',tax='1')
        self.seed_event(conflict)
        doc=document([self.first,self.second])
        before=self.dump()
        with self.assertRaises(LedgerError):self.actions.import_notice('A',doc)
        self.assertEqual(self.dump(),before)
        self.assertNotIn('activity_documents',self.tables())

    def test_owner_mismatch_stops_all_items_without_creating_completion_tables(self):
        doc=document([self.first,self.second],owner='Different Example')
        before=self.dump()
        with self.assertRaises(LedgerError):self.actions.import_notice('A',doc)
        self.assertEqual(self.dump(),before)
        self.assertNotIn('activity_documents',self.tables())

    def trigger_second_item_failure(self):
        self.store.db.execute(
            "CREATE TRIGGER synthetic_notice_failure BEFORE INSERT ON activities "
            "WHEN NEW.event_key='"+key(self.second)+"' "
            "BEGIN SELECT RAISE(ABORT,'synthetic second item write failure'); END")
        self.store.db.commit()

    def test_second_database_write_failure_rolls_back_events_and_lazy_schema(self):
        self.trigger_second_item_failure()
        before=self.dump()
        with self.assertRaises((LedgerError,sqlite3.DatabaseError)):
            self.actions.import_notice('A',document([self.first,self.second]))
        self.assertEqual(self.dump(),before)
        self.assertNotIn('activity_documents',self.tables())
        self.assertNotIn('activity_file_events',self.tables())

    def test_catchup_database_failure_preserves_old_anchor_payload_and_no_completion_proof(self):
        doc=document([self.first,self.second]);self.seed_partial(doc)
        self.trigger_second_item_failure()
        before=self.dump()
        with self.assertRaises((LedgerError,sqlite3.DatabaseError)):
            self.actions.import_notice('A',doc)
        self.assertEqual(self.dump(),before)
        self.assertFalse(self.actions.notice_is_complete('A',doc.digest))

    def test_manual_cash_reconciliation_hashes_actual_persisted_payload(self):
        self.actions.add_cash('A','税還付','2026-10-29','100','Anonymous manual refund')
        before=self.stored()[0]
        incoming=dict(type='税還付',date='2026-10-29',name='Anonymous PDF refund',
                      code='',amount='100',currency='JPY',description='Parsed refund')
        doc=document([incoming])
        preview=self.actions.import_notice('A',doc)
        self.assertTrue(preview['same_event'])
        self.assertEqual(preview['matching_id'],before['id'])
        after=self.stored()[0]
        self.assertEqual(after['id'],before['id'])
        self.assertEqual(after['payload'],before['payload'])
        _,manifest=self.manifest(doc)
        self.assertEqual(manifest,[dict(event_key=before['event_key'],
                                        payload_sha256=canonical_hash(json.loads(before['payload'])))])
        self.assert_complete(doc)

    def test_manual_split_reconciliation_keeps_original_allocations_and_payload(self):
        original=dict(type='株式分割',date='2026-10-05',name='Anonymous manual split',
                      code='9900',before='5',added='5',after='10',ratio_old=1,ratio_new=2,
                      allocations={'NISA(成)':['5','5','10']},currency='JPY',
                      manual=True,evidence='Anonymous synthetic evidence')
        ident=self.seed_event(original);before=self.stored()[0]
        incoming={field:value for field,value in original.items()
                  if field not in ('allocations','manual','evidence')}
        incoming['name']='Anonymous parsed split'
        doc=document([incoming])
        preview=self.actions.import_notice('A',doc)
        self.assertTrue(preview['same_event'])
        self.assertEqual(preview['matching_id'],ident)
        self.assertEqual(self.stored()[0]['payload'],before['payload'])
        self.assert_complete(doc)

    def test_missing_link_never_counts_as_a_complete_multi_item_notice(self):
        doc=document([self.first,self.second]);self.actions.import_notice('A',doc)
        second_id=self.stored()[1]['id']
        with self.store.db:self.store.db.execute(
            'DELETE FROM activity_file_events WHERE account=? AND digest=? AND activity_id=?',
            ('A',doc.digest,second_id))
        self.assertFalse(self.actions.notice_is_complete('A',doc.digest))
        self.assertNotIn(doc.digest,self.actions.completed_notice_digests('A'))

    def test_wrong_expected_item_count_or_old_parser_revision_is_not_complete(self):
        doc=document([self.first,self.second]);self.actions.import_notice('A',doc)
        with self.store.db:self.store.db.execute(
            'UPDATE activity_documents SET item_count=1 WHERE account=? AND digest=?',('A',doc.digest))
        self.assertFalse(self.actions.notice_is_complete('A',doc.digest))
        with self.store.db:self.store.db.execute(
            'UPDATE activity_documents SET item_count=2,parser_revision=1 WHERE account=? AND digest=?',('A',doc.digest))
        self.assertFalse(self.actions.notice_is_complete('A',doc.digest))

    def test_manifest_duplicate_or_wrong_hash_is_not_a_complete_proof(self):
        doc=document([self.first,self.second]);self.actions.import_notice('A',doc)
        _,manifest=self.manifest(doc)
        for invalid in ([manifest[0],manifest[0]],
                        [dict(manifest[0],payload_sha256='0'*64),manifest[1]]):
            with self.subTest(invalid_kind='duplicate' if invalid[0]==invalid[1] else 'hash'):
                with self.store.db:self.store.db.execute(
                    'UPDATE activity_documents SET manifest=? WHERE account=? AND digest=?',
                    (json.dumps(invalid), 'A',doc.digest))
                self.assertFalse(self.actions.notice_is_complete('A',doc.digest))

    def test_full_payload_integrity_includes_fields_outside_the_original_event_key(self):
        doc=document([self.first,self.second]);self.actions.import_notice('A',doc)
        row=self.stored()[1];changed=json.loads(row['payload']);changed['unexpected_note']='tampered'
        self.assertEqual(key(changed),row['event_key'])
        with self.store.db:self.store.db.execute(
            'UPDATE activities SET payload=? WHERE id=?',(json.dumps(changed),row['id']))
        self.assertFalse(self.actions.notice_is_complete('A',doc.digest))

    def test_cross_account_activity_link_cannot_complete_another_accounts_document(self):
        doc=document([self.first,self.second]);self.actions.import_notice('A',doc)
        foreign=self.seed_event(self.second,'B');second_id=self.stored()[1]['id']
        with self.store.db:
            self.store.db.execute(
                'DELETE FROM activity_file_events WHERE account=? AND digest=? AND activity_id=?',
                ('A',doc.digest,second_id))
            self.store.db.execute('INSERT INTO activity_file_events(account,digest,activity_id) VALUES(?,?,?)',
                                  ('A',doc.digest,foreign))
        self.assertFalse(self.actions.notice_is_complete('A',doc.digest))

    def test_anchor_must_belong_to_the_verified_mapped_items(self):
        doc=document([self.first,self.second]);self.actions.import_notice('A',doc)
        outsider=self.seed_event(dividend('9902'))
        with self.store.db:self.store.db.execute(
            'UPDATE activity_files SET activity_id=? WHERE account=? AND digest=?',
            (outsider,'A',doc.digest))
        self.assertFalse(self.actions.notice_is_complete('A',doc.digest))
        before=self.dump()
        with self.assertRaises(LedgerError):self.actions.import_notice('A',doc)
        self.assertEqual(self.dump(),before)


if __name__=='__main__':unittest.main()
