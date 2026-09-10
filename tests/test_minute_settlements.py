import copy
import importlib.util
import json
from datetime import datetime
from pathlib import Path
import tempfile
import unittest


def records(day='2026-09-09', opening=100, closing=110):
    return {'AAA': [dict(timestamp=f'{day}T{clock}:00-04:00', open=value,
                         high=value, low=value, close=value, volume=1000)
                    for clock, value in [('09:31', opening), ('15:55', closing)]]}


class MinuteStoreTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('shaq_daily_oracle.minute_settlements'),
                             'Dedicated immutable minute observation store is missing')
        from shaq_daily_oracle.minute_settlements import MinuteStore
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MinuteStore(Path(self.tmp.name))

    def observe(self, day='2026-09-09', at='2026-09-09T16:10:00-04:00', data=None):
        return self.store.observe(day, ['AAA'], records(day) if data is None else data,
                                  provider='yfinance', observed_at=datetime.fromisoformat(at))

    def test_independent_later_trading_day_required_and_all_observations_retained(self):
        self.assertEqual(self.observe()['status'], 'provisional')
        self.assertEqual(self.observe(at='2026-09-09T17:00:00-04:00')['status'], 'provisional')
        final = self.observe(at='2026-09-10T09:00:00-04:00')
        self.assertEqual(final['status'], 'final')
        self.assertEqual(len(list(Path(self.tmp.name).rglob('observations/*.json'))), 3)
        self.assertEqual(self.observe(at='2026-09-10T10:00:00-04:00')['status'], 'final')

    def test_weekend_is_not_independent_trading_day(self):
        self.observe()
        self.assertEqual(self.observe(at='2026-09-12T10:00:00-04:00')['status'], 'provisional')

    def test_future_fields_irrelevant_but_target_revision_requires_new_confirmation(self):
        self.observe()
        data = records()
        data['AAA'][0].update(high=99999, close=88888, volume=3000)
        first = self.observe(at='2026-09-10T09:00:00-04:00', data=data)
        self.assertEqual(first['status'], 'final')
        data['AAA'][1]['open'] = 120
        revised = self.observe(at='2026-09-10T10:00:00-04:00', data=data)
        self.assertEqual(revised['status'], 'provisional')
        self.assertTrue(revised['correction'])
        self.assertNotEqual(first['execution_sha256'], revised['execution_sha256'])
        self.assertEqual(self.observe(at='2026-09-11T10:00:00-04:00', data=data)['status'], 'final')

    def test_zero_volume_revision_is_execution_relevant(self):
        self.observe()
        data = records(); data['AAA'][0]['volume'] = 0
        self.assertEqual(self.observe(at='2026-09-10T09:00:00-04:00', data=data)['status'], 'provisional')

    def test_duplicate_timestamp_ambiguous_target_is_unavailable(self):
        data = records(); data['AAA'].append(dict(data['AAA'][0]))
        result = self.observe(data=data)
        self.assertIsNone(result['targets']['AAA']['entry'])

    def test_tampering_is_rejected_and_shared_subset_uses_same_observation(self):
        data = records(); data['BBB'] = copy.deepcopy(data['AAA'])
        self.store.observe('2026-09-09', ['AAA', 'BBB'], data, provider='yfinance',
                           observed_at=datetime.fromisoformat('2026-09-09T16:10:00-04:00'))
        a = self.store.snapshot('2026-09-09', ['AAA'])
        b = self.store.snapshot('2026-09-09', ['BBB'])
        self.assertEqual(a['observation_hashes'], b['observation_hashes'])
        file = next(Path(self.tmp.name).rglob('observations/*.json'))
        value = json.loads(file.read_text()); value['records']['AAA'][0]['open'] = 999
        file.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            self.store.snapshot('2026-09-09', ['AAA'])

    def test_empty_predictions_require_no_observations_even_with_unrelated_corruption(self):
        self.observe()
        path = next(Path(self.tmp.name).rglob('observations/*.json'))
        path.write_text('invalid unrelated observation')
        result = self.store.snapshot('2026-09-09', [])
        self.assertEqual(result['status'], 'not_required')
        self.assertEqual(result['observation_hashes'], [])


if __name__ == '__main__':
    unittest.main()
