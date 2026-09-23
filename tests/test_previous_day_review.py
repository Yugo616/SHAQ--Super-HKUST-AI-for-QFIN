import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from shaq_daily_oracle.minute_settlements import settlement_due_dates


class PreviousDayReviewTests(unittest.TestCase):
    def test_provisional_prices_rechecked_same_evening_not_next_close(self):
        rows=[{'trade_date':'2026-09-22','minute':{'status':'provisional'},
               'labels':{'AAA':{'status':'provisional'}}}]
        attempted={'2026-09-22':{'scheduled_offsets':[5], 'last_app_open_date':'2026-09-22'}}
        now=datetime(2026,9,22,16,15,tzinfo=ZoneInfo('America/New_York'))
        self.assertEqual(settlement_due_dates(rows,now,attempted,app_open=True),['2026-09-22'])

    def test_previous_day_missing_prices_checked_before_today_starts(self):
        rows=[{'trade_date':'2026-09-22','minute':{'status':'pending'},'labels':{}}]
        attempted={'2026-09-22':{'scheduled_offsets':[5,15,30,60], 'last_app_open_date':'2026-09-22'}}
        now=datetime(2026,9,23,7,30,tzinfo=ZoneInfo('America/New_York'))
        self.assertEqual(settlement_due_dates(rows,now,attempted,app_open=True),['2026-09-22'])
        attempted['2026-09-22']['last_app_open_date']='2026-09-23'
        self.assertEqual(settlement_due_dates(rows,now,attempted,app_open=True),[])

    def test_confirmed_records_do_not_generate_background_network_work(self):
        rows=[{'trade_date':'2026-09-22','minute':{'status':'final'},'labels':{'AAA':{'status':'final'}}}]
        now=datetime(2026,9,23,7,30,tzinfo=ZoneInfo('America/New_York'))
        self.assertEqual(settlement_due_dates(rows,now,{},app_open=True),[])
