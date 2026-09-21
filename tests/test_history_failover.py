"""Daily acquisition survives partial failures without relabelling stale data."""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import yfinance as yf
from yfinance.exceptions import YFRateLimitError

from shaq_daily_oracle.data_providers import DataProfile, DataProviderError, YFinanceProvider
from shaq_daily_oracle.public_data import DailyBarCache


class HistoryRecoveryTests(unittest.TestCase):
    def test_daily_cache_identity_ignores_screening_and_execution_settings(self):
        from dataclasses import replace
        profile = DataProfile('one', 'universe-a')
        self.assertTrue(callable(getattr(profile, 'history_identity', None)))
        other = replace(profile, profile_id='two', universe_file='universe-b', maximum_candidates=3,
                        intraday_interval='1m', yahoo_worker_timeout_seconds=1234, request_timeout_seconds=20)
        self.assertEqual(profile.history_identity(), other.history_identity())
        openbb = replace(profile, market_provider='openbb-rest', openbb_base_url='https://provider.invalid',
                         openbb_routes={'daily_bars': '/historical'})
        self.assertNotEqual(profile.history_identity(), openbb.history_identity())
        self.assertNotEqual(openbb.history_identity(),
                            replace(openbb, openbb_routes={'daily_bars': '/other'}).history_identity())

    def test_partial_history_becomes_incremental_cache_for_next_day(self):
        # If the outer cache drops a failed group's successes, AAA downloads
        # the entire year again on day two instead of the seven-day overlap.
        calls = []
        broken = True
        def history(ticker, **kwargs):
            calls.append((ticker.ticker, kwargs['start']))
            if ticker.ticker == 'BBB' and broken:
                raise TimeoutError()
            day = '2026-09-18' if broken else '2026-09-21'
            return pd.DataFrame({'Open': [10], 'Close': [11], 'Volume': [3]},
                                index=pd.DatetimeIndex([day], tz='America/New_York'))
        with tempfile.TemporaryDirectory() as directory, patch.object(yf.Ticker, 'history', history):
            root = Path(directory)
            provider = YFinanceProvider(DataProfile('test', 'unused', yahoo_request_max_retries=0))
            provider.history_checkpoint_root = root / 'day1'
            with patch.object(provider, 'history', side_effect=lambda symbols, **kwargs:
                              provider._history_inline(symbols, session=None, **kwargs)):
                cache = DailyBarCache(provider, root / 'cache', overlap_days=7)
                with self.assertRaises(DataProviderError):
                    cache.history(['AAA', 'BBB'], start=date(2025, 8, 17), end=date(2026, 9, 21))
                broken = False
                provider.history_checkpoint_root = root / 'day2'
                result = cache.history(['AAA', 'BBB'], start=date(2025, 8, 18), end=date(2026, 9, 22))
            self.assertEqual(calls, [('AAA', '2025-08-17'), ('BBB', '2025-08-17'),
                                    ('AAA', '2026-09-11'), ('BBB', '2025-08-18')])
            self.assertEqual([r['close'] for r in result['AAA']], [11, 11])

    def test_rate_limit_stops_group_instead_of_hammering_remaining_symbols(self):
        calls = []
        def history(ticker, **kwargs):
            calls.append(ticker.ticker)
            raise YFRateLimitError()
        provider = YFinanceProvider(DataProfile('test', 'unused', yahoo_request_max_retries=0))
        with patch.object(yf.Ticker, 'history', history), self.assertRaises(DataProviderError):
            provider._history_inline(['AAA', 'BBB', 'CCC'], start=date(2026, 9, 1),
                                     end=date(2026, 9, 21), session=None)
        self.assertEqual(calls, ['AAA'])

    def test_corrupt_incremental_cache_is_not_accepted(self):
        calls = []
        class Provider:
            def history(self, symbols, **kwargs):
                calls.append(kwargs['start'])
                return {'AAA': [{'timestamp': '2026-09-18T00:00:00', 'close': 10}]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = DailyBarCache(Provider(), root, overlap_days=7)
            cache.history(['AAA'], start=date(2025, 8, 17), end=date(2026, 9, 21))
            path = next(root.glob('*.json'))
            value = json.loads(path.read_text())
            value['rows'][0]['close'] = 999
            path.write_text(json.dumps(value))
            result = cache.history(['AAA'], start=date(2025, 8, 18), end=date(2026, 9, 22))
            self.assertEqual(calls[-1], date(2025, 8, 18))
            self.assertEqual(result['AAA'][0]['close'], 10)

    def test_gap_before_cached_range_is_not_claimed_as_covered(self):
        calls = []
        class Provider:
            def history(self, symbols, **kwargs):
                calls.append(kwargs['start'])
                day = '2026-09-18' if len(calls) == 1 else '2026-08-20'
                return {'AAA': [{'timestamp': day, 'close': 10}]}
        with tempfile.TemporaryDirectory() as directory:
            cache = DailyBarCache(Provider(), Path(directory), overlap_days=7)
            cache.history(['AAA'], start=date(2026, 9, 1), end=date(2026, 9, 21))
            cache.history(['AAA'], start=date(2026, 8, 1), end=date(2026, 8, 21))
            cache.history(['AAA'], start=date(2026, 8, 1), end=date(2026, 9, 22))
            self.assertLessEqual(calls[-1], date(2026, 8, 20))


if __name__ == '__main__':
    unittest.main()
