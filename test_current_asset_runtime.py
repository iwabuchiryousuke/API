"""Deterministic current-view settling failures and latest-display behavior."""
import unittest
from unittest.mock import Mock,patch
from ledger import LedgerError
from sbi_asset_runtime import read_current_view


class SettlingTests(unittest.TestCase):
    def run_view(self,renderer,busy=lambda _:False):
        clock=[0.0];page=Mock();page.url='https://site3.sbisec.co.jp/summary'
        page.wait_for_timeout.side_effect=lambda ms:clock.__setitem__(0,round(clock[0]+ms/1000,6))
        with patch('sbi_asset_runtime.time.monotonic',side_effect=lambda:clock[0]),patch('sbi_asset_runtime.current_view_busy',side_effect=lambda _:busy(clock[0])),patch('sbi_views.snapshot_dom',side_effect=lambda _:(renderer(clock[0]),[])),patch('sbi_views._check_asset_identity'):
            return read_current_view(page,'口座サマリー',timeout=1,stable_seconds=.3)

    def test_late_changed_amount_invalidates_earlier_stable_partial(self):
        with self.assertRaises(LedgerError):
            self.run_view(lambda t:'現金残高等(合計)\n'+('100' if t<.8 else '200'))

    def test_late_invalid_display_invalidates_earlier_stable_partial(self):
        with self.assertRaises(LedgerError):
            self.run_view(lambda t:'現金残高等(合計)\n100' if t<.8 else '口座管理')

    def test_new_loading_state_invalidates_earlier_stable_partial(self):
        with self.assertRaises(LedgerError):
            self.run_view(lambda t:'現金残高等(合計)\n100',busy=lambda t:t>=.8)

    def test_latest_changed_amount_is_returned_after_it_stabilizes(self):
        _,_,values=self.run_view(lambda t:'現金残高等(合計)\n'+('100' if t<.4 else '200'))
        self.assertEqual(values['cash_total'],200)


if __name__=='__main__':unittest.main()
