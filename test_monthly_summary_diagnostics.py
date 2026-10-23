"""Summary conflicts have fixed diagnostics and never persist a measurement."""
from contextlib import ExitStack
import unittest
from unittest.mock import Mock,patch

from auto_login import AcquisitionError
from monthly_summary import capture_summary
from sbi_views import ObservationConflict


class MonthlySummaryDiagnostics(unittest.TestCase):
    def test_conflicting_summary_has_fixed_error_code_before_observation_write(self):
        page=Mock();store=Mock()
        with ExitStack() as stack:
            stack.enter_context(patch('sbi_views.select_asset_page',return_value=page))
            stack.enter_context(patch('sbi_views._check_asset_identity'))
            stack.enter_context(patch('monthly_summary._snapshot',return_value=[('保有資産評価\n株式\n100',[])]))
            stack.enter_context(patch('monthly_summary._read_stable',side_effect=ObservationConflict('anonymous input value or URL must not be copied')))
            save=stack.enter_context(patch('sbi_views.save_observation'))
            with self.assertRaises(AcquisitionError) as error:
                capture_summary(page,store,'A',owner='Anonymous A',identity_verified=True)
        self.assertEqual(error.exception.code,'SUMMARY_VALUES_CONFLICT')
        self.assertNotIn('anonymous input',str(error.exception))
        save.assert_not_called()


if __name__=='__main__':unittest.main()
