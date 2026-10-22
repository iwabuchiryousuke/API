"""Display states must distinguish pending values, dates, native USD and blanks."""
import unittest
from asset_dialog import amount,current_rows,boundary_rows,history_rows,display_stamp

class AssetDialogRows(unittest.TestCase):
    def test_zero_is_visible_and_unknown_is_never_zero(self):
        self.assertEqual(amount(0),'0');self.assertEqual(amount(None),'未取得');self.assertEqual(amount(1250.5),'1,250.5')

    def test_actual_times_are_displayed_in_jst_and_missing_zone_is_not_invented(self):
        self.assertEqual(display_stamp('2026-10-21T15:00:00+00:00'),'2026-10-22 00:00:00')
        self.assertEqual(display_stamp('2026-10-22T09:00:00'),'時刻未確認')

    def test_pending_current_rows_retain_native_usd_and_independent_source_dates(self):
        accounts=[{'id':'A','label':'Anonymous A'}]
        observations={('A','口座サマリー'):{'observed_at':'2026-10-21T23:00:00+09:00','values':{'cash_total':123,'growth_remaining':{'year':2025,'remaining':50}}},
                      ('A','My資産'):{'observed_at':'2026-10-22T09:00:00+09:00','values':{'domestic':100,'usd_cash':2.5}}}
        row=current_rows({'observations':observations,'pending_accounts':['A']},accounts,'2026-10-22')[0]
        self.assertIn('未反映',row[1]);self.assertIn('以前の取得',row[1]);self.assertEqual(row[6],'123')
        self.assertEqual(row[7],'未取得');self.assertEqual(row[8],'2.5');self.assertIn('2025年',row[9])
        self.assertEqual(row[4],'未取得');self.assertEqual(row[11],'100')

    def test_manual_blank_has_priority_over_observed_candidate_and_missing_day_is_explicit(self):
        accounts=[{'id':'A','label':'Anonymous A'}];day='2026-09-30'
        preview={'snapshots':{('A',day):{'source':'手動確認','domestic':100,'note':'Manually confirmed'}},
                 'boundary_candidates':{'A':{day:{'available':['growth_remaining'],'values':{'growth_remaining':999}}}}}
        rows=boundary_rows(preview,accounts,(day,'2026-10-01'))
        self.assertEqual(rows[0][2],'手動確認');self.assertEqual(rows[0][7],'未取得')
        self.assertEqual(rows[1][2],'未取得');self.assertIn('復元しません',rows[1][-1])

    def test_history_displays_observed_and_displayed_times_separately(self):
        preview={'history_observations':[{'account':'A','kind':'My資産','observed_at':'2026-10-22T09:00:00+09:00','payload':{'displayed_at':'2026/10/21 17:00','values':{'domestic':0}}}]}
        row=history_rows(preview,[{'id':'A','label':'Anonymous A'}])[0]
        self.assertIn('2026-10-22',row[2]);self.assertIn('2026/10/21',row[3]);self.assertEqual(row[4],'国内区分の表示分=0円')

    def test_empty_legacy_dated_row_is_not_replaced_visually_by_an_intraday_candidate(self):
        day='2026-09-30'
        preview={'snapshots':{('A',day):{'source':'SBI My資産履歴','domestic':None,'note':'Kept dated row'}},
                 'boundary_candidates':{'A':{day:{'available':['domestic'],'values':{'domestic':999}}}}}
        row=boundary_rows(preview,[{'id':'A','label':'Anonymous A'}],(day,))[0]
        self.assertEqual(row[3],'未取得');self.assertEqual(row[-1],'Kept dated row')

    def test_legacy_derived_timestamp_is_not_displayed_as_a_verified_baseline(self):
        day='2026-10-01'
        row=boundary_rows({'snapshots':{('A',day):{'source':'SBI My資産の前月比','domestic':100,'note':'Old baseline claim'}}},[{'id':'A','label':'Anonymous A'}],(day,))[0]
        self.assertEqual(row[2],'旧版の前月比推定');self.assertIn('指定日残高は未確認',row[-1])

if __name__=='__main__':unittest.main()
