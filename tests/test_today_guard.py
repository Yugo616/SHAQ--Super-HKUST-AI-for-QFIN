"""Today must not silently turn closed/stale sessions into historical research."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from shaq_daily_oracle.lab_service import LabService, LabServiceError
from shaq_daily_oracle.data_providers import DataProviderError
from shaq_daily_oracle.research_collection import collect_research_evidence, ResearchCollectionError
import test_research_lab_foundation as foundation
import test_research_collection as collection
from test_research_collection import FakeMarket, FakeMetadata, FakeEvents, PACKAGE_ROOT

ET = ZoneInfo('America/New_York')


class MondayMarket(FakeMarket):
    def recent_intraday(self, symbols, *, cutoff):
        return {s: [{"timestamp": "2026-09-14T08:40:00-04:00", "close": 110, "volume": 100}]
                for s in symbols}


class TodayGuardTests(unittest.TestCase):
    def collect(self, root, market, now, **kwargs):
        return collect_research_evidence(root=root, package_root=PACKAGE_ROOT,
            profile=collection.ResearchCollectionTests().profile(), sec_identity='Research test@example.edu',
            market_provider=market, metadata_provider=FakeMetadata(), event_provider=FakeEvents(),
            observed_at=now, **kwargs)

    def test_manual_backend_rejects_closed_and_pre04_before_settings_or_jobs(self):
        for stamp, reason in [('2026-09-13T08:45:00-04:00', '休市'),
                              ('2026-09-07T08:45:00-04:00', '休市'),
                              ('2026-09-14T08:00:00+08:00', '休市'),
                              ('2026-09-14T03:59:00-04:00', '04:00')]:
            with self.subTest(stamp=stamp), tempfile.TemporaryDirectory() as name:
                lab = LabService(foundation.ResearchLabFoundationTests().paths(Path(name)))
                with patch('shaq_daily_oracle.lab_service.datetime') as clock, \
                        patch.object(lab.settings, 'model_profile', side_effect=AssertionError('credential path reached')):
                    clock.now.return_value = datetime.fromisoformat(stamp).astimezone(ET)
                    with self.assertRaisesRegex(LabServiceError, reason):
                        lab.start_batch(selections=[])
                self.assertEqual(lab.jobs, {})
                self.assertFalse((lab.paths.research_root/'evidence_sessions').exists())

    def test_today_evidence_rechecks_calendar_before_cached_locator(self):
        with tempfile.TemporaryDirectory() as name:
            lab = LabService(foundation.ResearchLabFoundationTests().paths(Path(name)))
            with patch('shaq_daily_oracle.lab_service.datetime') as clock, \
                    patch('shaq_daily_oracle.lab_service.collect_research_evidence', side_effect=AssertionError('collection reached')):
                clock.now.return_value = datetime(2026, 9, 13, 8, 45, tzinfo=ET)
                with self.assertRaisesRegex(LabServiceError, '休市'):
                    lab._today_evidence(profile=collection.ResearchCollectionTests().profile(), sec_identity='test')

    def test_all_previous_friday_or_empty_minutes_rejected_without_frozen_bundle(self):
        empty = MondayMarket()
        empty.recent_intraday = lambda symbols, cutoff: {s: [] for s in symbols}
        for market in [FakeMarket(), empty]:
            with self.subTest(market=market), tempfile.TemporaryDirectory() as name:
                root = Path(name)/'evidence'
                with self.assertRaisesRegex(ResearchCollectionError, 'no_data.*未取得当天盘前数据'):
                    self.collect(root, market, datetime(2026, 9, 14, 8, 45, tzinfo=ET))
                self.assertFalse(root.exists())

    def test_observed_provider_exception_is_not_empty_data(self):
        market = MondayMarket()
        market.recent_intraday = lambda *a, **k: (_ for _ in ()).throw(DataProviderError('provider failed'))
        with tempfile.TemporaryDirectory() as name:
            with self.assertRaisesRegex(ResearchCollectionError, 'provider_error'):
                self.collect(Path(name)/'e', market, datetime(2026, 9, 14, 8, 45, tzinfo=ET))

    def test_nonpositive_prices_are_not_valid_current_day_observations(self):
        class InvalidMarket(MondayMarket):
            def recent_intraday(self, symbols, *, cutoff):
                return {s: [{**row, 'close': 0}] for s, rows in super().recent_intraday(symbols, cutoff=cutoff).items() for row in rows}
        with tempfile.TemporaryDirectory() as name:
            with self.assertRaisesRegex(ResearchCollectionError, 'no_data'):
                self.collect(Path(name)/'e', InvalidMarket(), datetime(2026, 9, 14, 8, 45, tzinfo=ET))

    def test_normal_monday_partial_data_preserves_observation_status_and_late_scope(self):
        class PartialMarket(MondayMarket):
            def recent_intraday(self, symbols, *, cutoff):
                return {s: rows if s == 'AAPL' else []
                        for s, rows in super().recent_intraday(symbols, cutoff=cutoff).items()}
        for hour, status in [(8, 'on_time'), (10, 'late_research_only')]:
            with self.subTest(hour=hour), tempfile.TemporaryDirectory() as name:
                evidence = self.collect(Path(name)/'e', PartialMarket(), datetime(2026, 9, 14, hour, 45, tzinfo=ET))
                self.assertEqual(evidence.manifest['cutoff_status'], status)
                observations = evidence.manifest['provider_manifest']['premarket_observations']
                self.assertEqual(observations['AAPL']['status'], 'collected')
                self.assertTrue(any(row['status'] == 'no_data' for row in observations.values()))

    def test_old_cached_previous_session_is_rejected_without_collection(self):
        from shaq_daily_oracle.hashing import sha256_payload
        with tempfile.TemporaryDirectory() as name:
            lab = LabService(foundation.ResearchLabFoundationTests().paths(Path(name)))
            evidence = self.collect(lab.paths.research_root/'evidence'/'old', FakeMarket(),
                                    datetime(2026, 9, 4, 8, 45, tzinfo=ET))
            profile = collection.ResearchCollectionTests().profile()
            locator = lab.paths.research_root/'evidence_sessions'/f'2026-09-14-{profile.identity()[:12]}-{sha256_payload({})[:12]}.json'
            locator.parent.mkdir()
            locator.write_text(json.dumps({'evidence_hash':'old'}))
            before = (evidence.root/'evidence_manifest.json').read_bytes()
            with patch('shaq_daily_oracle.lab_service.datetime') as clock, \
                    patch('shaq_daily_oracle.lab_service.collect_research_evidence', side_effect=AssertionError('must reject cache')):
                clock.now.return_value = datetime(2026, 9, 14, 8, 45, tzinfo=ET)
                with self.assertRaisesRegex(ResearchCollectionError, 'cached|当天'):
                    lab._today_evidence(profile=profile, sec_identity='test')
            self.assertEqual((evidence.root/'evidence_manifest.json').read_bytes(), before)

    def test_today_job_stops_before_llm_with_previous_session_minutes(self):
        with tempfile.TemporaryDirectory() as name:
            lab = LabService(foundation.ResearchLabFoundationTests().paths(Path(name)))
            now = datetime(2026, 9, 14, 8, 45, tzinfo=ET)
            def collect(**kwargs):
                self.assertFalse(kwargs['allow_replay'])
                return self.collect(kwargs['root'], FakeMarket(), now)
            with patch('shaq_daily_oracle.lab_service.datetime') as clock, \
                    patch('shaq_daily_oracle.lab_service.collect_research_evidence', side_effect=collect), \
                    patch('shaq_daily_oracle.lab_service.ResearchBatchRunner', side_effect=AssertionError('LLM runner reached')):
                clock.now.return_value = now
                lab._run_batch_job(job_id='fixture', variants=[], profile=None, secret='')
            self.assertEqual(lab.jobs['fixture']['status'], 'failed')
            self.assertIn('no_data', lab.jobs['fixture']['message'])
            self.assertEqual(list(lab.paths.batches_root.iterdir()), [])

    def test_today_collect_and_valid_same_day_cache_reuse_do_not_replay(self):
        with tempfile.TemporaryDirectory() as name:
            lab = LabService(foundation.ResearchLabFoundationTests().paths(Path(name)))
            now = datetime(2026, 9, 14, 8, 45, tzinfo=ET)
            def collect(**kwargs):
                self.assertFalse(kwargs['allow_replay'])
                return self.collect(kwargs['root'], MondayMarket(), now)
            with patch('shaq_daily_oracle.lab_service.datetime') as clock, \
                    patch('shaq_daily_oracle.lab_service.collect_research_evidence', side_effect=collect):
                clock.now.return_value = now
                original = lab._today_evidence(profile=collection.ResearchCollectionTests().profile(), sec_identity='test')
            with patch('shaq_daily_oracle.lab_service.datetime') as clock, \
                    patch('shaq_daily_oracle.lab_service.collect_research_evidence', side_effect=AssertionError('cache was not used')):
                clock.now.return_value = now
                reused = lab._today_evidence(profile=collection.ResearchCollectionTests().profile(), sec_identity='test')
            self.assertEqual(original.manifest, reused.manifest)

    def test_cache_requires_same_day_premarket_observations_and_exact_cutoff(self):
        import copy
        from dataclasses import replace
        from shaq_daily_oracle.research_collection import validate_today_evidence
        with tempfile.TemporaryDirectory() as name:
            now = datetime(2026, 9, 14, 8, 45, tzinfo=ET)
            evidence = self.collect(Path(name)/'e', MondayMarket(), now)
            for field, value in [('scheduled_cutoff_et', '2026-09-14T09:30:00-04:00'),
                                 ('as_of_et', '2026-09-14T09:00:00-04:00'),
                                 ('provider_manifest', {'premarket_observations': {}}),
                                 ('provider_manifest', {'premarket_observations': {'AAPL': {
                                     'status': 'collected', 'first_observation_et': '2026-09-11T08:00:00-04:00',
                                     'last_observation_et': '2026-09-11T08:40:00-04:00'}}})]:
                manifest = copy.deepcopy(evidence.manifest)
                manifest[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ResearchCollectionError):
                    validate_today_evidence(replace(evidence, manifest=manifest), now)
