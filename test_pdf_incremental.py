"""Incremental archive matching never trusts old filenames on their own."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from epost_collector import (_HeadingIndex, _header_pair, _legacy_header_matches,
                             _verify_document, save_pdf)
from test_document_classification import japanese_pdf as _japanese_pdf
from test_epost_reliability import pdf_bytes as _pdf_bytes


def card(identity='one', *, title='取引報告書', day='2026/10/01', fingerprint=None, stable_id=''):
    return dict(key='node:' + identity, date=day, title=title, stable_id=stable_id,
                fingerprint=fingerprint or day + ' ' + title)


class HeadingIndexTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.item = card()
        self.raw = _pdf_bytes('source-one')
        self.path, _ = save_pdf(self.root, 'A', self.item['date'], self.item['title'], self.raw)

    def index(self, items=None, complete=True, owner='Example A', account='A', scope=''):
        return _HeadingIndex(self.root, account, items or [self.item],
                             complete_list=complete, expected_owner=owner, source_scope=scope)

    def store(self, item=None):
        index = self.index()
        index.put(item or self.item, self.path, self.raw)
        index.flush(commit=True)

    def test_complete_run_without_machine_id_can_reuse_valid_pdf(self):
        self.store()
        found = self.index().get(card('new-dom-node'))
        self.assertEqual(found[:2], (self.path, self.raw))
        self.assertIn('一意', found[2])

    def test_owner_or_account_change_cannot_reuse_old_source_mapping(self):
        self.store()
        self.assertIsNone(self.index(owner='Example B').get(self.item))
        self.assertIsNone(self.index(account='B', owner='Example A').get(self.item))

    def test_different_filter_scope_does_not_reuse_a_known_heading(self):
        index = self.index(scope='first-scope-hash')
        index.put(self.item, self.path, self.raw)
        index.flush(commit=True)
        self.assertIsNone(self.index(scope='different-scope-hash').get(self.item))

    def test_unverified_manual_owner_cannot_reuse_heading_only_mapping(self):
        self.store()
        self.assertIsNone(self.index(owner=None).get(self.item))

    def test_known_stable_id_cache_is_owner_bound_even_when_paginated(self):
        stable = card(stable_id='document:one')
        index = self.index([stable], complete=False)
        index.put(stable, self.path, self.raw)
        index.flush(commit=True)
        self.assertEqual(self.index([stable], complete=False).get(stable)[:2], (self.path, self.raw))
        self.assertIsNone(self.index([stable], complete=False, owner='Example B').get(stable))

    def test_partial_run_never_establishes_a_new_mapping(self):
        index = self.index()
        index.put(self.item, self.path, self.raw)
        index.flush(commit=False)
        self.assertFalse(index.path.exists())

    def test_missing_or_changed_pdf_does_not_satisfy_cache(self):
        self.store()
        self.path.write_bytes(_pdf_bytes('changed-content'))
        index = self.index()
        self.assertIsNone(index.get(self.item))
        index.flush(commit=False)
        self.assertEqual(json.loads(index.path.read_text())['entries'], {})

    def test_same_date_title_duplicate_always_requires_download_without_source_id(self):
        self.store()
        second = card('two', fingerprint='different physical card')
        index = self.index([self.item, second])
        self.assertIsNone(index.get(self.item))
        self.assertIsNone(index.get(second))

    def test_incomplete_or_paginated_listing_does_not_use_heading_fallback(self):
        self.store()
        self.assertIsNone(self.index(complete=False).get(self.item))

    def test_changed_full_header_is_downloaded_not_backfilled_from_old_filename(self):
        self.store()
        changed = card(fingerprint='2026/10/01 取引報告書 訂正再交付')
        self.assertIsNone(self.index([changed]).get(changed))

    def test_header_pair_rejects_invalid_calendar_date_and_malformed_cache_values(self):
        self.assertIsNone(_header_pair({'date': 123, 'title': '取引報告書'}))
        self.assertIsNone(_header_pair(card(day='2026/02/30')))
        self.assertIsNone(_header_pair(card(title='')))

    def test_legacy_pdf_requires_exact_readable_title_date_and_configured_owner(self):
        candidate = self.root / 'generated.pdf'
        _japanese_pdf(candidate, '作成日 2026年10月1日 取引報告書 Example A 様')
        raw = candidate.read_bytes()
        self.assertTrue(_legacy_header_matches(raw, self.item, 'ExampleA'))
        self.assertFalse(_legacy_header_matches(raw, card(day='2026/10/02'), 'ExampleA'))
        self.assertFalse(_legacy_header_matches(raw, card(title='取引残高報告書'), 'ExampleA'))
        self.assertFalse(_legacy_header_matches(raw, self.item, 'ExampleB'))
        self.assertFalse(_legacy_header_matches(self.raw, self.item, 'ExampleA'))

    def test_verified_legacy_header_bootstraps_index_without_download(self):
        generated = self.root / 'generated.pdf'
        _japanese_pdf(generated, '作成日 2026年10月1日 取引報告書 Example A 様')
        raw = generated.read_bytes()
        self.path.unlink()
        path, _ = save_pdf(self.root, 'A', self.item['date'], self.item['title'], raw)
        index = self.index()
        self.assertEqual(index.get(self.item)[:2], (path, raw))
        self.assertEqual(index.bootstrap, 1)
        index.flush(commit=True)
        self.assertEqual(self.index().get(self.item)[:2], (path, raw))

    def test_mislabeled_legacy_filename_does_not_skip_remote_pdf(self):
        generated = self.root / 'generated.pdf'
        _japanese_pdf(generated, '2026年9月1日 取引残高報告書 Example A 様')
        raw = generated.read_bytes()
        save_pdf(self.root, 'A', self.item['date'], self.item['title'], raw)
        self.assertIsNone(self.index().get(self.item))

    def test_ambiguous_legacy_content_versions_are_not_chosen_by_filename(self):
        self.path.unlink()
        for number in (1, 2):
            generated = self.root / ('generated' + str(number) + '.pdf')
            _japanese_pdf(generated, '作成日 2026年10月1日 取引報告書 Example A 様 改版' + str(number))
            save_pdf(self.root, 'A', self.item['date'], self.item['title'], generated.read_bytes())
        self.assertIsNone(self.index().get(self.item))

    def test_symlink_cache_file_cannot_supply_a_reusable_pdf(self):
        if not hasattr(os, 'symlink'):
            self.skipTest('Symlinks are unavailable')
        self.store()
        original = self.path.read_bytes()
        self.path.unlink()
        outside = self.root / 'outside.pdf'
        outside.write_bytes(original)
        try:
            self.path.symlink_to(outside)
        except OSError:
            self.skipTest('Creating symlinks is not permitted')
        self.assertIsNone(self.index().get(self.item))

    def test_incidental_trade_date_is_not_proof_of_delivery_or_creation_date(self):
        generated = self.root / 'date-in-table.pdf'
        _japanese_pdf(generated, '取引報告書 Example A 様 約定日 2026年10月1日')
        self.assertFalse(_legacy_header_matches(generated.read_bytes(), self.item, 'ExampleA'))

    def test_owner_name_prefix_is_not_an_exact_holder_match(self):
        generated = self.root / 'holder.pdf'
        _japanese_pdf(generated, '作成日2026年10月1日 取引報告書 Example AB 様')
        self.assertFalse(_legacy_header_matches(generated.read_bytes(), self.item, 'ExampleA'))

    def test_malformed_source_index_falls_back_to_retrieval(self):
        index = self.index()
        index.path.write_text('{"version":2,"account":"A","owner":"ExampleA","entries":[]}', encoding='utf-8')
        self.assertIsNone(self.index().get(self.item))

    def test_unknown_valid_pdf_remains_archivable_but_known_kind_conflict_is_rejected(self):
        self.assertEqual(_verify_document(self.raw, card(title='口座約款の改定について')),
                         '本文の書類名は未確認')
        path = self.root / 'balance.pdf'
        _japanese_pdf(path, '2026年10月1日 取引残高報告書 Example A 様')
        with self.assertRaisesRegex(ValueError, '一致しません'):
            _verify_document(path.read_bytes(), self.item)


if __name__ == '__main__':
    unittest.main()
