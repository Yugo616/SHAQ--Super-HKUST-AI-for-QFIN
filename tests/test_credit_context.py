import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from shaq_daily_oracle import public_data


class CreditContextTests(unittest.TestCase):
    def test_missing_and_future_observations_are_not_spreads(self):
        source = {'series_id':'BAMLH0A0HYM2','units':'percent', 'history_rows':30}
        raw = 'observation_date,BAMLH0A0HYM2\n2026-10-05,3.12\n2026-10-06,3.03\n2026-10-07,.\n2026-10-08,2.5\n'
        cutoff = datetime(2026,10,8,8,50,tzinfo=ZoneInfo('America/New_York'))
        result = public_data.parse_fred_context(raw,source=source,cutoff=cutoff)
        self.assertEqual(result['observations'][-1], {'date':'2026-10-06','value':3.03})
        self.assertEqual(result['units'],'percent')
        self.assertEqual(result['latest_change_basis_points'],-9)
        self.assertFalse(result['intraday'])

    def test_wrong_series_and_non_finite_data_rejected(self):
        cutoff=datetime(2026,10,8,8,50,tzinfo=ZoneInfo('America/New_York'))
        for raw in ('observation_date,OTHER\n2026-10-06,1\n',
                    'observation_date,BAMLH0A0HYM2\n2026-10-06,nan\n'):
            with self.assertRaises(ValueError):
                public_data.parse_fred_context(raw,source={'series_id':'BAMLH0A0HYM2','units':'percent','history_rows':30},cutoff=cutoff)
