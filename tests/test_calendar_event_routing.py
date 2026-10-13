import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx

import test_research_batch as batch_fixtures
import test_research_collection as collection_fixtures
from shaq_daily_oracle.hashing import sha256_payload
from shaq_daily_oracle.research_batch import (
    ContentAddressedModelCache,
    IncompleteDomainError,
    _run_domain,
    _tasks_for_domain,
)
from shaq_daily_oracle.research_collection import collect_research_evidence


class PrimaryFiling(collection_fixtures.FakeEvents):
    def recent_events(self, members, **kwargs):
        return {m.symbol: [{'status': 'collected', 'form': '8-K',
            'accession_number': '000123-26-000001',
            'acceptance_time': '2026-09-04T07:00:00-04:00',
            'source_url': 'https://www.sec.gov/Archives/edgar/data/123/00012326000001/main.htm'}]
            if m.symbol == 'AAPL' else [] for m in members}

    def download_primary_document(self, url):
        return b'<html><p>Revenue 123 million. Outlook 140 million.</p></html>'


class CalendarEventRoutingTests(unittest.TestCase):
    def collect(self, root, *, calendar=True, events=None):
        class Market(collection_fixtures.FakeMarket):
            def __init__(self, profile):
                self.profile = profile
                self.history_checkpoint_root = None

        def public_response(source, name, cutoff, config):
            if name == 'nasdaq_earnings':
                return httpx.Response(200, json={'data': {'rows': [{
                    'symbol': 'AAPL', 'name': 'Fixture company', 'time': 'time-after-hours',
                    'epsForecast': '1.50', 'noOfEsts': '5', 'fiscalQuarterEnding': 'Jun/2026',
                    'epsActual': '99.00', 'epsSurprise': 'future result',
                }] if calendar else []}})
            if source['kind'] == 'fred_csv':
                body = f"observation_date,{source['series_id']}\n2026-09-03,1.25\n"
            elif source['kind'] == 'html':
                body = '<table><tr><td>2026-09-03</td><td>4.5</td></tr></table>'
            else:
                body = 'DATE,CLOSE\n09/03/2026,15.0\n'
            return httpx.Response(200, text=body)

        now = datetime(2026, 9, 4, 8, 45, tzinfo=ZoneInfo('America/New_York'))
        with patch('shaq_daily_oracle.research_collection.YFinanceProvider', Market), patch(
                'shaq_daily_oracle.public_history_recovery.wrap_public_recovery',
                side_effect=lambda provider, *args, **kwargs: provider), patch(
                'shaq_daily_oracle.public_data._public_response', side_effect=public_response):
            return collect_research_evidence(
                root=root/'evidence', package_root=collection_fixtures.PACKAGE_ROOT,
                profile=collection_fixtures.ResearchCollectionTests().profile(),
                sec_identity='Research test@example.edu', observed_at=now, collection_clock=lambda: now,
                metadata_provider=collection_fixtures.FakeMetadata(),
                event_provider=events or collection_fixtures.FakeEvents())

    def run_event(self, root, evidence, *, verdict='neutral', calls=None,
                  cited_providers=('nasdaq_earnings', 'sec-edgar')):
        helper = batch_fixtures.ResearchBatchTests()
        registry = helper.registry(root)
        variant = helper.main_variant(registry)
        model = batch_fixtures.FakeModel()

        def caller(**kwargs):
            tasks = json.loads(kwargs['prompt'].split('FROZEN TASKS:\n')[1])
            if calls is not None:
                calls.extend(tasks)
            value, audit = model(**kwargs)
            for row, task in zip(value['results'], tasks, strict=True):
                row['report']['verdict'] = verdict
                row['report']['evidence_ids'] = [r['evidence_id'] for r in task['evidence']
                    if r['provider'] in cited_providers]
            audit['output_sha256'] = sha256_payload(value)
            return value, audit

        return _run_domain(domain='event', evidence=evidence,
            documents=registry.effective_skills(variant.version_id, variant.author),
            profile=helper.profile(), secret='', cache=ContentAddressedModelCache(root/'cache'),
            caller=caller, prompt_format_version=3)

    def test_calendar_reaches_only_matching_event_task_without_becoming_primary_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = self.collect(root)
            tasks = {t['symbol']: t for t in _tasks_for_domain(evidence, 'event', prompt_format_version=3)}
            self.assertEqual(tasks['AAPL']['collection_status'], 'collected')
            calendar = next(r for r in evidence.lineage['records'] if r['provider'] == 'nasdaq_earnings')
            self.assertEqual(calendar['domain'], 'event')
            self.assertEqual(calendar['root_component_type'], 'event_calendar')
            self.assertFalse(next(c for c in evidence.candidate_intake['candidates']
                                  if c['symbol'] == 'AAPL')['captured_primary_event'])
            for symbol, task in tasks.items():
                if symbol != 'AAPL':
                    self.assertEqual(task['collection_status'], 'not_applicable')
                    self.assertNotIn(calendar['evidence_id'], [r['evidence_id'] for r in task['evidence']])
            packet = next(r['content'] for r in tasks['AAPL']['evidence'] if r['provider'] == 'nasdaq_earnings')
            self.assertEqual(packet['data'][0]['epsForecast'], '1.50')
            self.assertNotIn('epsActual', json.dumps(packet))
            self.assertNotIn('epsSurprise', json.dumps(packet))
            self.assertFalse(packet['directional_event_eligible'])
            self.assertIn('scheduled', packet['interpretation_boundary'])
            calls = []
            reports, audits = self.run_event(root, evidence, calls=calls)
            self.assertEqual([t['symbol'] for t in calls], ['AAPL'])
            by_symbol = dict(zip(tasks, reports, strict=True))
            self.assertEqual(by_symbol['AAPL']['verdict'], 'neutral')
            self.assertEqual(by_symbol['AAPL']['evidence_ids'], [calendar['evidence_id']])
            self.assertEqual(len(audits), 1)

    def test_calendar_only_cannot_supply_a_directional_company_event(self):
        for direction in ('bullish', 'bearish'):
            with self.subTest(direction=direction), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                evidence = self.collect(root)
                with self.assertRaises(IncompleteDomainError) as failure:
                    self.run_event(root, evidence, verdict=direction)
                self.assertEqual(set(failure.exception.failures), {'AAPL'})
                self.assertIn('calendar', failure.exception.failures['AAPL']['message'])

    def test_broad_market_context_does_not_create_a_company_event(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = self.collect(root, calendar=False)
            calls = []
            reports, audits = self.run_event(root, evidence, calls=calls)
            self.assertEqual(calls, [])
            self.assertEqual(audits, [])
            self.assertTrue(all(r['verdict'] == 'not_applicable' for r in reports))

    def test_primary_filing_with_calendar_can_still_support_direction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = self.collect(root, events=PrimaryFiling())
            reports, _ = self.run_event(root, evidence, verdict='bullish')
            self.assertTrue(any(r['verdict'] == 'bullish' for r in reports))

    def test_calendar_citation_alone_cannot_borrow_direction_from_an_uncited_filing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = self.collect(root, events=PrimaryFiling())
            with self.assertRaises(IncompleteDomainError) as failure:
                self.run_event(root, evidence, verdict='bullish', cited_providers=('nasdaq_earnings',))
            self.assertIn('calendar', failure.exception.failures['AAPL']['message'])
