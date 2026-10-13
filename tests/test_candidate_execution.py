import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import test_research_batch as fixtures
from shaq_daily_oracle.contracts import DOMAINS
from shaq_daily_oracle.model_execution import ExecutionPolicy
from shaq_daily_oracle.research_batch import (
    ContentAddressedModelCache,
    ResearchBatchError,
    ResearchBatchRunner,
    freeze_evidence_bundle,
    load_frozen_evidence,
    run_variant,
)
from shaq_daily_oracle.research_dashboard import ResearchDashboardIndex


def full_domain_evidence(root, symbols=('AAPL', 'MSFT')):
    stamp = '2026-09-04T08:49:00-04:00'
    types = {'market': 'market_context', 'capital': 'capital_flow',
        'derivatives': 'stock_derivatives', 'event': 'stock_event',
        'price_volume': 'stock_price_volume', 'relationships': 'industry_context'}
    records, files = [], {}
    for symbol in symbols:
        for domain, component in types.items():
            path = f'raw/{symbol}-{domain}.json'
            files[path] = json.dumps({'symbol': symbol, 'domain': domain}).encode()
            records.append(dict(
                evidence_id=f'ev_{"market" if domain == "market" else "price"}_{symbol}_{domain}',
                domain=domain, provider='fixture', source_uri=f'https://example.test/{symbol}/{domain}',
                captured_at=stamp, raw_file_path=path, scope_symbols=[symbol],
                consumer_domains=[domain], root_component_type=component))
    evidence = freeze_evidence_bundle(root=root/'staged', as_of_et=stamp,
        scheduled_cutoff_et=stamp, cutoff_status='on_time',
        candidates=[{'symbol': s} for s in symbols], records=records,
        files=files, provider_manifest={'profile_id': 'fixture'})
    target = root/'evidence'/evidence.manifest['evidence_hash']
    target.parent.mkdir()
    evidence.root.replace(target)
    return load_frozen_evidence(target)


class CandidateExecutionTests(unittest.TestCase):
    def test_format_two_retains_domain_order_and_original_groups_after_a_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = fixtures.ResearchBatchTests()
            registry = helper.registry(root)
            evidence = full_domain_evidence(root)
            model, calls = fixtures.FakeModel(), []

            def fail_capital(**kwargs):
                tasks = json.loads(kwargs['prompt'].split('FROZEN TASKS:\n')[1])
                calls.append((tasks[0]['domain'], tuple(t['symbol'] for t in tasks)))
                if tasks[0]['domain'] == 'capital':
                    raise TimeoutError('legacy group timeout')
                return model(**kwargs)

            with self.assertRaises(ResearchBatchError):
                run_variant(variant=helper.main_variant(registry), evidence=evidence,
                    registry=registry, profile=helper.profile(), secret='',
                    cache=ContentAddressedModelCache(root/'cache'), integration_policy=helper.policy(),
                    output_root=root/'variants', caller=fail_capital, prompt_format_version=2)
            self.assertEqual(calls, [
                ('capital', ('AAPL', 'MSFT')), ('capital', ('AAPL', 'MSFT')),
                ('derivatives', ('AAPL', 'MSFT')), ('event', ('AAPL', 'MSFT')),
                ('market', ('AAPL', 'MSFT')), ('price_volume', ('AAPL',)),
                ('price_volume', ('MSFT',)), ('relationships', ('AAPL', 'MSFT'))])

    def test_early_failure_skips_later_fresh_work_and_recovers_only_missing_reports(self):
        for failing_domain in ('capital', 'derivatives'):
            with self.subTest(domain=failing_domain), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                helper = fixtures.ResearchBatchTests()
                registry = helper.registry(root)
                evidence = full_domain_evidence(root)
                runner = ResearchBatchRunner(batches_root=root/'batches', cache_root=root/'cache',
                    registry=registry, integration_policy=helper.policy())
                profile = replace(helper.profile(), rate_limit_per_minute=1000)
                policy = ExecutionPolicy(timeout_seconds=1, transient_retries=1, citation_retries=1)
                model = fixtures.FakeModel()
                calls = []

                def fail_early(**kwargs):
                    if 'FROZEN TASKS:\n' in kwargs['prompt']:
                        task = json.loads(kwargs['prompt'].split('FROZEN TASKS:\n')[1])[0]
                        calls.append((task['symbol'], task['domain']))
                        if task['symbol'] == 'AAPL' and task['domain'] == failing_domain:
                            raise TimeoutError('fixture timeout')
                    return model(**kwargs)

                first = runner.run(evidence=evidence, variants=[helper.main_variant(registry)],
                    profile=profile, secret='', caller=fail_early, execution_policy=policy)
                result = first['results']['team/main']
                self.assertEqual(set(result['excluded_candidates']), {'AAPL'})
                self.assertEqual([p['symbol'] for p in result['predictions']], ['MSFT'])
                self.assertEqual(calls.count(('AAPL', failing_domain)), 2)
                later = {d for d in DOMAINS if d > failing_domain}
                self.assertEqual([d for s, d in calls if s == 'AAPL' and d in later], [])
                prior = {d for d in DOMAINS if d < failing_domain}
                self.assertEqual({r['domain'] for r in result['reports_by_symbol']['AAPL']}, prior)
                batch = Path(first['batch_root'])
                result_path = next((batch/'variants').glob('*/variant_result.json'))
                original = result_path.read_bytes()
                checkpoints = {p: p.read_bytes() for p in (batch/'model_calls').glob('*.json')}
                recovery_calls = []

                def recover(**kwargs):
                    if 'FROZEN TASKS:\n' in kwargs['prompt']:
                        tasks = json.loads(kwargs['prompt'].split('FROZEN TASKS:\n')[1])
                        recovery_calls.extend((t['symbol'], t['domain']) for t in tasks)
                    return model(**kwargs)

                resumed = runner.resume(batch_id=batch.name, evidence=evidence, profile=profile,
                    secret='', caller=recover, execution_policy=policy)
                self.assertTrue(resumed['status']['all_variants_completed'])
                self.assertEqual(set(recovery_calls), {('AAPL', d) for d in DOMAINS - prior})
                self.assertEqual(len(recovery_calls), len(DOMAINS - prior))
                self.assertEqual(result_path.read_bytes(), original)
                for path, content in checkpoints.items():
                    self.assertEqual(path.read_bytes(), content)
                index = ResearchDashboardIndex(batches_root=root/'batches', database=root/'index.db')
                supplement = index.batch_detail(batch.name)['candidate_recoveries']['team/main']
                self.assertEqual({r['domain'] for r in supplement['reports_by_symbol']['AAPL']}, DOMAINS)
