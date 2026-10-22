"""Anonymous fixtures protect the supplied Excel contract and upgrade data safety.

The workbook hashes were generated with the unmodified uploaded v3.5.11 app;
ZIP container timestamps are excluded. No user documents or credentials are used.
"""
import hashlib
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from zipfile import ZipFile

from activity import Activities
from asset_snapshots import save
from ledger import Snapshot, Store
from report_model import build
from standalone_excel import write_model
from update_workflow import Cancelled, Workflow


def seed_fixture(base):
    store = Store(Path(base) / 'data')
    activities = Activities(store)
    for account in 'ABCDE':
        store.rename(account, 'Account ' + account)
        store.set_owner(account, 'Example ' + account)
    trades = [
        dict(trade_date='2026-09-01', settlement_date='2026-09-03',
             name='Example Security', code='9900', market='東証', side='買',
             custody='NISA', tax_class='', quantity='100', price='125.5',
             fees='0', tax='0', settlement_amount='12550'),
        dict(trade_date='2026-10-02', settlement_date='2026-10-06',
             name='Example Security', code='9900', market='東証', side='売',
             custody='NISA', tax_class='', quantity='25', price='150',
             fees='0', tax='0', settlement_amount='3750'),
    ]
    raw = json.dumps(trades, sort_keys=True).encode()
    snapshot = Snapshot(raw, 'anonymous.csv', hashlib.sha256(raw).hexdigest(),
                        '2026-09-01', '2026-10-31', trades)
    store.import_snapshot('A', snapshot)
    activities.add_cash('A', '配当', '2026-10-15', '100', 'Example Security',
                        code='9900', custody='NISA', quantity='100', per_share='1')
    activities.add_cash('D', '利息', '2026-10-01', '12', 'Example Interest')
    save(store, 'A', '2026-09-30',
         dict(domestic=10000, foreign=20000, cash=30000, foreign_cash=40000,
              growth_remaining=50000, tsumitate_remaining=60000),
         source='Fixture', note='Verified date')
    save(store, 'A', '2026-10-01',
         dict(domestic=10001, foreign=20001, cash=30001, foreign_cash=40001,
              growth_remaining=50001, tsumitate_remaining=60001),
         source='Fixture', note='Verified date')
    market = store.directory / 'market'
    market.mkdir(exist_ok=True)
    (market / 'research_2026.json').write_text(
        json.dumps({'year': 2026, 'securities': {}}), encoding='utf8')
    return store, activities


def seed_observations(store):
    from sbi_views import init_observations
    init_observations(store)
    stamp = '2026-10-22T13:00:00+09:00'
    observations = {
        '口座サマリー': dict(cash_total=30001, broker_cash=10000, bank=20001,
                       growth_remaining={'year': 2026, 'remaining': 50001},
                       tsumitate_remaining={'year': 2026, 'remaining': 60001}),
        'My資産': dict(domestic=10001, foreign=20001, cash=10000,
                     bank=20001, foreign_cash=40001),
    }
    with store.db:
        for kind, values in observations.items():
            payload = dict(values=values, observed_at=stamp,
                           displayed_at='2026/10/22 13:00', source=kind)
            store.db.execute('INSERT INTO account_observations VALUES (?,?,?,?)',
                             ('A', kind, stamp, json.dumps(payload, ensure_ascii=False)))


def workbook_hashes(path):
    with ZipFile(path) as archive:
        return {name: hashlib.sha256(archive.read(name)).hexdigest()
                for name in archive.namelist()}


# Exact OOXML payloads from the user's original v3.5.11, with a fixed report
# month and highlight date. Update only after explicit Excel contract changes.
BASELINE_OOXML = {'[Content_Types].xml': 'e5381eeed8ea0ca56d966fa50ee25026f25d3dadfeb345d4986c7bdb4e51a2d6',
 '_rels/.rels': 'fc5715a86467973c771fed6cf49203ed48734dcbd0f24088be6bbb6e290cd7b5',
 'xl/workbook.xml': 'bafc01191783a6b62c5030a98c452c13ffa797ed7af24062e293a4d72396ea72',
 'xl/_rels/workbook.xml.rels': 'bcfa3e0223b870b69b71dad322f7a7e2095089d2bd23936b89cf5f0a74b4c334',
 'xl/styles.xml': '0428cdd974530d1fd415fd7aba32adbe51767ef780a79996cba404fdf79afd4c',
 'xl/worksheets/sheet1.xml': 'e0162f4bb1b5725769cb5a56f839d4913075e32a91729202337248fed073572a',
 'xl/worksheets/sheet2.xml': 'e95405ac13f0142eeded08c1ca6dd7d52f5b3d02afb247c9d75341b744f47245',
 'xl/worksheets/sheet3.xml': 'b3bb276afec03ed9160eab1a0a832d38df86f9e8fd84bbc6f725f9e3cc0ca5df'}


BASELINE_OBSERVED_MANAGEMENT = {'[Content_Types].xml': '1db0e6a9f5ec1fc1a43f97cb9fdad5be3036944586df243c4929bd86b6ccff2d',
 '_rels/.rels': 'fc5715a86467973c771fed6cf49203ed48734dcbd0f24088be6bbb6e290cd7b5',
 'xl/workbook.xml': 'ae2c192da88e6aa4affddbc5174929a2cbdcd9101109ceea529a10840262ba45',
 'xl/_rels/workbook.xml.rels': '7e3e3d5dc1cca5b27e988442d09740be7b5b0d88ef6af9cc3f50efff52b42c67',
 'xl/styles.xml': '497589611781f0a9cd346e478cee41e6016b1d2f205cefc6b769b47b45d96968',
 'xl/worksheets/sheet1.xml': '717acb382a6465d14711ca676d6ebc48f0ba27e1597d99ba02a6af5f1f59e03d',
 'xl/worksheets/sheet2.xml': 'e95405ac13f0142eeded08c1ca6dd7d52f5b3d02afb247c9d75341b744f47245',
 'xl/worksheets/sheet3.xml': '68fb944a058215f3ba000b9821919b161a975c471fa0390eea1c42178d90ccce',
 'xl/worksheets/sheet4.xml': '0a40824e7e1098e2eb8e557381fc815179010bbf3594d5ef3f869d27581d090d',
 'xl/worksheets/_rels/sheet3.xml.rels': 'b81c1a6af34e9c741185b2c12bceab1781b43739a35210e99f70ef212f96721c',
 'xl/drawings/_rels/drawing1.xml.rels': '5ef8692f50853d943025f7cef440bae5be0697c0ee09a1183b1f8e79f6d10496',
 'xl/drawings/drawing1.xml': '39d90fee6a19a45dc9129f2b30dd493a17b448aca56b6e650731d2e6dc23190a',
 'xl/charts/chart1.xml': '30f2051e1c247654d908bee895ee267d094ed5fed828efa36efe1e7d6c5caceb'}


class ExcelContract(unittest.TestCase):
    def test_original_excel_format_and_formulas_are_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            store, activities = seed_fixture(temp)
            try:
                model = build(store, activities, '2026-10',
                              today=date(2026, 10, 22))
                path = write_model(model, Path(temp) / 'output')
                self.assertEqual(workbook_hashes(path), BASELINE_OOXML)
            finally:
                store.close()

    def test_observed_assets_and_management_workbook_keep_original_format(self):
        with tempfile.TemporaryDirectory() as temp:
            store, activities = seed_fixture(temp)
            try:
                seed_observations(store)
                model = build(store, activities, '2026-10', management=True,
                              today=date(2026, 10, 22))
                path = write_model(model, Path(temp) / 'output', management=True)
                self.assertEqual(workbook_hashes(path), BASELINE_OBSERVED_MANAGEMENT)
            finally:
                store.close()


class UpgradeDataSafety(unittest.TestCase):
    def test_repeat_export_and_review_cancel_preserve_settings_and_records(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            store, _ = seed_fixture(base)
            # Unknown future tables and opaque private files belong to the user.
            with store.db:
                store.db.execute('CREATE TABLE user_preferences (name TEXT PRIMARY KEY, value TEXT)')
                store.db.execute('INSERT INTO user_preferences VALUES (?,?)',
                                 ('custom-layout', 'keep-this-value'))
            before_records = store.records()
            before_accounts = store.accounts()
            before_dump = '\n'.join(store.db.iterdump())
            store.close()
            private = base / 'data' / 'private' / 'login_A.dat'
            private.parent.mkdir()
            private.write_bytes(b'opaque-test-only-settings')
            for _ in range(2):
                result = Workflow(base).run('2026-10', review=lambda *a: True)
                self.assertTrue(result['output'].is_file())
                self.assertEqual(result['summary']['new_trades'], 0)
                self.assertEqual(result['summary']['new_cash_or_splits'], 0)
            with self.assertRaises(Cancelled):
                Workflow(base).run('2026-10', review=lambda *a: False)
            store = Store(base / 'data')
            try:
                self.assertEqual(store.records(), before_records)
                self.assertEqual(store.accounts(), before_accounts)
                self.assertEqual(store.db.execute(
                    'SELECT value FROM user_preferences WHERE name=?',
                    ('custom-layout',)).fetchone()[0], 'keep-this-value')
                self.assertEqual(private.read_bytes(), b'opaque-test-only-settings')
                # Readiness may add the observation table, but every existing
                # persisted value must remain identical throughout the update.
                for statement in before_dump.splitlines():
                    if statement.startswith('INSERT INTO'):
                        self.assertIn(statement, '\n'.join(store.db.iterdump()))
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
