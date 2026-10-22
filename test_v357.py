import unittest
from unittest.mock import Mock,patch
from datetime import date
import pandas as pd
from yahoo_market import fetch,merge_reference,previous_close,number
from sbi_navigation import open_post,controls
from login_credentials import CredentialError

class YahooTests(unittest.TestCase):
 def ticker(self):
  t=Mock();t.history.return_value=pd.DataFrame({'Close':[100,110],'Dividends':[0,3]},index=pd.to_datetime(['2025-09-29','2025-09-30']));t.calendar={'Ex-Dividend Date':date(2025,12,29),'Dividend Date':date(2026,3,1)};return t
 def test_dividends_are_ex_dates_not_payments(self):
  r=fetch('6501',2025,lambda _:self.ticker());self.assertEqual(r['historical_dividends'],[{'ex_date':'2025-09-30','dps':3}]);self.assertNotIn('events',r);self.assertNotIn('dps',r)
 def test_no_overwrite_verified(self):
  r=fetch('6501',2025,lambda _:self.ticker());old={'dps':20,'events':[{'dps':10}],'payments':'2025-06-01'};merged=merge_reference(old,r,2025)
  for key in old:self.assertEqual(old[key],merged[key])
 def test_next_year_payment(self):
  r=fetch('6501',2025,lambda _:self.ticker());self.assertEqual(merge_reference({},r,2025)['payments'],'2026-03-01')
 def test_close_unadjusted(self):
  t=self.ticker();r=previous_close('6501','2025-10',lambda _:t);self.assertEqual(r['close'],110);self.assertFalse(t.history.call_args.kwargs['auto_adjust'])
 def test_missing_not_zero(self):
  self.assertIsNone(number(float('nan')));t=self.ticker();t.history.return_value=pd.DataFrame()
  with self.assertRaises(ValueError):fetch('6501',2025,lambda _:t)

if __name__=='__main__':unittest.main()
