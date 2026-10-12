import copy
import json
import tempfile
import unittest
from pathlib import Path

import test_research_batch as fixtures
from shaq_daily_oracle.research_batch import (
    freeze_evidence_bundle, _tasks_for_domain, _run_domain,
    ContentAddressedModelCache, ResearchBatchError,
    authenticated_failed_checkpoints,
)
from shaq_daily_oracle.model_backends import ModelProfile


class SupplementReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fixture = fixtures.ResearchBatchTests()
        self.old = self.fixture.evidence(self.root)

    def supplemented(self, source=None, consumers=None):
        manifest = self.old.manifest
        return freeze_evidence_bundle(
            root=self.root/'supplement', as_of_et='2026-09-04T08:55:00-04:00',
            scheduled_cutoff_et=manifest['scheduled_cutoff_et'], cutoff_status='late_research_only',
            candidates=manifest['candidate_intake']['candidates'],
            records=manifest['records'] + [{
                'evidence_id': 'ev_pressure', 'domain': 'capital', 'provider': 'test-quotes',
                'source_uri': 'https://example.test/quotes',
                'captured_at': '2026-09-04T08:49:30-04:00',
                'raw_file_path': 'raw/pressure.json', 'scope_symbols': ['AAPL'],
                'consumer_domains': consumers or ['capital', 'price_volume'], 'root_component_type': 'capital_flow'}],
            files={**{p:(self.old.root/p).read_bytes() for p in manifest['file_sha256']},
                   'raw/pressure.json': b'{"pressure": 0.1}'},
            provider_manifest={'task_reuse_source_manifest': source or manifest})

    def test_unchanged_market_keeps_original_task_and_observation_time(self):
        new = self.supplemented()
        self.assertEqual(_tasks_for_domain(new, 'market'), _tasks_for_domain(self.old, 'market'))
        self.assertEqual(new.manifest['as_of_et'], '2026-09-04T08:55:00-04:00')

    def test_supplement_checkpoint_authentication_uses_frozen_documents_without_storage(self):
        registry=self.fixture.registry(self.root)
        docs=registry.effective_skills('main','baseline')
        profile=self.fixture.profile(); cache=ContentAddressedModelCache(self.root/'cache')
        model=fixtures.FakeModel()
        _run_domain(domain='market', evidence=self.old, documents=docs,
            profile=profile,secret='',cache=cache,caller=model)
        calls={}
        for path in (self.root/'cache').rglob('*.json'):
            row=json.loads(path.read_text())
            if 'cache_key' in row: calls[row['cache_key']]=row
        manifest={'model_profile':profile.public_dict(), 'batch_identity':{
            'model_profile_hash':profile.identity(),'execution_config':{'prompt_format_version':1}}}
        authenticated=authenticated_failed_checkpoints(evidence=self.supplemented(),manifest=manifest,
            skill_snapshots={'baseline/main':{'documents':docs}},failed_variants={'baseline/main':{}},calls=calls)
        self.assertEqual(authenticated,set(calls))

    def test_changed_price_task_cannot_reuse_original_identity(self):
        new = self.supplemented()
        self.assertNotEqual(_tasks_for_domain(new, 'price_volume')[0]['task_id'],
                            _tasks_for_domain(self.old, 'price_volume')[0]['task_id'])

    def test_tampered_source_manifest_is_rejected(self):
        source = copy.deepcopy(self.old.manifest)
        source['as_of_et'] = '2026-09-04T08:48:00-04:00'
        new = self.supplemented(source)
        with self.assertRaises(ResearchBatchError):
            _tasks_for_domain(new, 'market')

    def test_real_cache_reuses_unchanged_domain_without_model_call(self):
        registry = self.fixture.registry(self.root)
        docs = registry.effective_skills('main', 'baseline')
        profile = ModelProfile(profile_id='test', protocol='openai-responses',
                               model='test-model', base_url='https://example.test/v1')
        cache = ContentAddressedModelCache(self.root/'cache')
        model = fixtures.FakeModel()
        first, _ = _run_domain(domain='market', evidence=self.old, documents=docs,
            profile=profile, secret='fixture', cache=cache, caller=model)
        count = model.calls
        second, audits = _run_domain(domain='market', evidence=self.supplemented(), documents=docs,
            profile=profile, secret='fixture', cache=cache, caller=model)
        self.assertEqual(model.calls, count)
        self.assertEqual(first, second)
        self.assertTrue(all(a['cache_hit'] for a in audits))

    def test_changed_stock_does_not_recall_unchanged_stock_from_grouped_result(self):
        m = self.old.manifest
        self.old = freeze_evidence_bundle(root=self.root/'two-stocks',
            as_of_et=m['as_of_et'], scheduled_cutoff_et=m['scheduled_cutoff_et'],
            cutoff_status='on_time', candidates=m['candidate_intake']['candidates'] + [{'symbol':'MSFT'}],
            records=m['records'], files={p:(self.old.root/p).read_bytes() for p in m['file_sha256']},
            provider_manifest=m['provider_manifest'])
        docs = self.fixture.registry(self.root).effective_skills('main', 'baseline')
        profile = ModelProfile(profile_id='test', protocol='openai-responses',
            model='test-model', base_url='https://example.test/v1')
        cache = ContentAddressedModelCache(self.root/'cache')
        model = fixtures.FakeModel()
        groups = []
        def caller(**kw):
            groups.append([t['symbol'] for t in json.loads(kw['prompt'].split('FROZEN TASKS:\n')[1])])
            return model(**kw)
        _run_domain(domain='market', evidence=self.old, documents=docs,
            profile=profile, secret='fixture', cache=cache, caller=caller)
        reports, _ = _run_domain(domain='market', evidence=self.supplemented(consumers=['capital','market']),
            documents=docs, profile=profile, secret='fixture', cache=cache, caller=caller)
        self.assertEqual(groups, [['AAPL','MSFT'], ['AAPL']])
        self.assertEqual(len(reports), 2)
