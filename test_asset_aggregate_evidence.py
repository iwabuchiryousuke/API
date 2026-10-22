"""Raw stock observations cannot establish unobserved aggregate components."""
import unittest
from asset_snapshots import combine_assets


class AggregateEvidence(unittest.TestCase):
    def test_stock_only_keeps_aggregate_unknown(self):
        values=combine_assets({'domestic':100,'foreign':200})
        self.assertIsNone(values['domestic']);self.assertIsNone(values['foreign'])

    def test_each_missing_or_unknown_component_prevents_aggregate(self):
        complete={'domestic':100,'fund':20,'bond':30,'foreign':200,'usd_fund':40,'usd_bond':50}
        for key in complete:
            with self.subTest(key=key):
                values=dict(complete);values.pop(key)
                aggregate='domestic' if key in ('domestic','fund','bond') else 'foreign'
                self.assertIsNone(combine_assets(values)[aggregate])
                values=dict(complete);values[key]=None
                self.assertIsNone(combine_assets(values)[aggregate])

    def test_explicit_zero_components_establish_complete_aggregate(self):
        values=combine_assets({'domestic':100,'fund':0,'bond':0,'foreign':200,'usd_fund':0,'usd_bond':0})
        self.assertEqual(values['domestic'],100);self.assertEqual(values['foreign'],200)

    def test_nonzero_components_sum_without_guessing(self):
        values=combine_assets({'domestic':100,'fund':20,'bond':30,'foreign':200,'usd_fund':40,'usd_bond':50,'cash':300,'bank':400,'foreign_cash':500})
        self.assertEqual(values,{'domestic':150,'foreign':290,'cash':700,'foreign_cash':500})

    def test_all_explicit_zeros_are_known_zero(self):
        values=combine_assets(dict.fromkeys(('domestic','fund','bond','foreign','usd_fund','usd_bond','cash','bank','foreign_cash'),0))
        self.assertEqual(values,{'domestic':0,'foreign':0,'cash':0,'foreign_cash':0})


if __name__=='__main__':unittest.main()
