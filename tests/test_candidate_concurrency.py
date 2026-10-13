import hashlib
import json
import tempfile
import threading
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import test_research_batch as fixtures
from test_candidate_execution import full_domain_evidence
from shaq_daily_oracle.contracts import DOMAINS
from shaq_daily_oracle.hashing import sha256_payload
from shaq_daily_oracle.model_execution import ExecutionPolicy
from shaq_daily_oracle.research_batch import ContentAddressedModelCache, ResearchBatchRunner
from shaq_daily_oracle.research_dashboard import ResearchDashboardIndex


class CandidateConcurrencyTests(unittest.TestCase):
    def test_recovery_stops_failed_candidate_while_other_pending_candidate_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = fixtures.ResearchBatchTests()
            registry = helper.registry(root)
            evidence = full_domain_evidence(root, symbols=('AAPL', 'MSFT', 'NVDA'))
            runner = ResearchBatchRunner(batches_root=root/'batches', cache_root=root/'cache',
                registry=registry, integration_policy=helper.policy())
            profile = replace(helper.profile(), max_concurrency=2, rate_limit_per_minute=1000)
            policy = ExecutionPolicy(transient_retries=0, citation_retries=0)
            model = fixtures.FakeModel()

            def initial(**kwargs):
                if 'FROZEN TASKS:\n' in kwargs['prompt']:
                    task = json.loads(kwargs['prompt'].split('FROZEN TASKS:\n')[1])[0]
                    if task['symbol'] in {'AAPL', 'MSFT'} and task['domain'] == 'capital':
                        raise TimeoutError('initial candidate failure')
                return model(**kwargs)

            first = runner.run(evidence=evidence, variants=[helper.main_variant(registry)],
                profile=profile, secret='', caller=initial, execution_policy=policy)
            self.assertEqual(set(first['results']['team/main']['excluded_candidates']), {'AAPL', 'MSFT'})
            batch = Path(first['batch_root'])
            result_path = next((batch/'variants').glob('*/variant_result.json'))
            original_result = result_path.read_bytes()
            original_calls = {p: p.read_bytes() for p in (batch/'model_calls').glob('*.json')}
            failed_started, release_failure, recovered_review = (threading.Event() for _ in range(3))
            calls, lock = [], threading.Lock()

            def partially_recover(**kwargs):
                prompt = kwargs['prompt']
                if 'FROZEN TASKS:\n' in prompt:
                    task = json.loads(prompt.split('FROZEN TASKS:\n')[1])[0]
                    with lock:
                        calls.append((task['symbol'], task['domain']))
                    if (task['symbol'], task['domain']) == ('AAPL', 'capital'):
                        failed_started.set()
                        if not release_failure.wait(10):
                            raise AssertionError('fixture failure was not released')
                        raise TimeoutError('candidate still unavailable')
                value = model(**kwargs)
                if 'REPORTS:\n' in prompt and set(json.loads(prompt.split('REPORTS:\n')[1])) == {'MSFT'}:
                    recovered_review.set()
                return value

            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(runner.resume, batch_id=batch.name, evidence=evidence,
                    profile=profile, secret='', caller=partially_recover, execution_policy=policy)
                try:
                    self.assertTrue(failed_started.wait(5))
                    other_finished = recovered_review.wait(3)
                finally:
                    release_failure.set()
                partial = future.result(timeout=10)
            with self.subTest(boundary='no later fresh work for failed recovery'):
                self.assertEqual([d for s, d in calls if s == 'AAPL'], ['capital'])
            with self.subTest(boundary='independent pending candidate completes'):
                self.assertTrue(other_finished, 'other pending candidate was blocked during recovery')
            self.assertEqual(Counter((s, d) for s, d in calls if s != 'AAPL'),
                             Counter({('MSFT', d): 1 for d in DOMAINS}))
            self.assertFalse(partial['status']['all_variants_completed'])
            self.assertEqual(result_path.read_bytes(), original_result)
            for path, content in original_calls.items():
                self.assertEqual(path.read_bytes(), content)
            index = ResearchDashboardIndex(batches_root=root/'batches', database=root/'index.sqlite3')
            detail = index.batch_detail(batch.name)
            self.assertEqual(set(detail['variants']), {'team/main'})
            self.assertEqual(detail['candidate_recoveries'], {})
            self.assertEqual(len(detail['model_calls']), 14)
            successful_calls = {p: p.read_bytes() for p in (batch/'model_calls').glob('*.json')}
            final_calls = []

            def recover_remaining(**kwargs):
                if 'FROZEN TASKS:\n' in kwargs['prompt']:
                    final_calls.extend(t['symbol'] for t in json.loads(kwargs['prompt'].split('FROZEN TASKS:\n')[1]))
                return model(**kwargs)

            final = runner.resume(batch_id=batch.name, evidence=evidence, profile=profile,
                secret='', caller=recover_remaining, execution_policy=policy)
            self.assertTrue(final['status']['all_variants_completed'])
            self.assertEqual(final_calls, ['AAPL'] * 6)
            self.assertEqual(result_path.read_bytes(), original_result)
            for path, content in successful_calls.items():
                self.assertEqual(path.read_bytes(), content)
            supplement = index.batch_detail(batch.name)['candidate_recoveries']['team/main']
            self.assertEqual(set(supplement['reports_by_symbol']), {'AAPL', 'MSFT'})

    def test_healthy_candidate_finishes_while_failed_call_is_still_waiting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = fixtures.ResearchBatchTests()
            registry = helper.registry(root)
            evidence = full_domain_evidence(root)
            runner = ResearchBatchRunner(batches_root=root/'batches', cache_root=root/'cache',
                registry=registry, integration_policy=helper.policy())
            profile = replace(helper.profile(), max_concurrency=2, rate_limit_per_minute=1000)
            failed_started, release_failure, healthy_review = (threading.Event() for _ in range(3))
            model = fixtures.FakeModel()
            lock = threading.Lock()
            calls, events = [], []

            def caller(**kwargs):
                prompt = kwargs['prompt']
                if 'FROZEN TASKS:\n' in prompt:
                    tasks = json.loads(prompt.split('FROZEN TASKS:\n')[1])
                    self.assertEqual(len(tasks), 1)
                    task = tasks[0]
                    with lock:
                        calls.append((task['symbol'], task['domain']))
                    if task['symbol'] == 'AAPL' and task['domain'] == 'capital':
                        failed_started.set()
                        if not release_failure.wait(10):
                            raise AssertionError('fixture failure was not released')
                        raise TimeoutError('fixture candidate timeout')
                value = model(**kwargs)
                if 'REPORTS:\n' in prompt and set(json.loads(prompt.split('REPORTS:\n')[1])) == {'MSFT'}:
                    healthy_review.set()
                return value

            def observe(**event):
                with lock:
                    events.append(event)

            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(runner.run, evidence=evidence,
                    variants=[helper.main_variant(registry)], profile=profile, secret='', caller=caller,
                    execution_policy=ExecutionPolicy(transient_retries=1), observer=observe)
                try:
                    self.assertTrue(failed_started.wait(5), 'failed candidate did not start')
                    self.assertTrue(healthy_review.wait(3),
                        'healthy candidate was blocked by an unrelated model call')
                    self.assertFalse(future.done(), 'unresolved candidate was silently dropped')
                    with lock:
                        self.assertEqual({d for s, d in calls if s == 'MSFT'}, DOMAINS)
                finally:
                    release_failure.set()
                first = future.result(timeout=10)
            result = first['results']['team/main']
            self.assertEqual(set(result['excluded_candidates']), {'AAPL'})
            self.assertEqual([p['symbol'] for p in result['predictions']], ['MSFT'])
            self.assertEqual(Counter(calls), Counter({('AAPL', 'capital'): 2,
                **{('MSFT', d): 1 for d in DOMAINS}}))
            retries = [e for e in events if e['stage'] in {
                'model_attempt_failed', 'model_retry_scheduled', 'model_retry_started'}]
            self.assertTrue(retries)
            self.assertTrue(all(e['symbols'] == ['AAPL'] and e['domain'] == 'capital' for e in retries))
            self.assertEqual(len({e['call_id'] for e in retries}), 1)
            batch = Path(first['batch_root'])
            saved = {p: p.read_bytes() for p in (batch/'model_calls').glob('*.json')}
            resumed_calls = []

            def recover(**kwargs):
                if 'FROZEN TASKS:\n' in kwargs['prompt']:
                    resumed_calls.extend((t['symbol'], t['domain'])
                        for t in json.loads(kwargs['prompt'].split('FROZEN TASKS:\n')[1]))
                return model(**kwargs)

            resumed = runner.resume(batch_id=batch.name, evidence=evidence, profile=profile,
                secret='', caller=recover)
            self.assertTrue(resumed['status']['all_variants_completed'])
            self.assertEqual(Counter(resumed_calls), Counter({('AAPL', d): 1 for d in DOMAINS}))
            for path, content in saved.items():
                self.assertEqual(path.read_bytes(), content)

    def test_model_cache_limits_concurrent_calls_across_independent_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = ContentAddressedModelCache(Path(directory)/'cache')
            profile = replace(fixtures.ResearchBatchTests().profile(),
                max_concurrency=2, rate_limit_per_minute=1000)
            release, capacity, overflow = (threading.Event() for _ in range(3))
            lock = threading.Lock()
            active = peak = 0

            def caller(*, profile, secret, prompt, schema):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                    if active == profile.max_concurrency:
                        capacity.set()
                    if active > profile.max_concurrency:
                        overflow.set()
                try:
                    if not release.wait(10):
                        raise AssertionError('fixture calls were not released')
                    result = {'prompt': prompt}
                    return result, {'profile_sha256': profile.identity(),
                        'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
                        'schema_sha256': sha256_payload(schema), 'output_sha256': sha256_payload(result)}
                finally:
                    with lock:
                        active -= 1

            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [executor.submit(cache.call, profile=profile, secret='', prompt=f'fixture {i}',
                    schema={'type': 'object'}, caller=caller) for i in range(4)]
                try:
                    self.assertTrue(capacity.wait(5))
                    self.assertFalse(overflow.wait(0.3), 'profile concurrency was exceeded')
                finally:
                    release.set()
                for future in futures:
                    future.result(timeout=10)
            self.assertEqual(peak, profile.max_concurrency)

    def test_two_methods_share_one_profile_concurrency_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = fixtures.ResearchBatchTests()
            registry = helper.registry(root)
            shadow = helper.install_shadow(registry, version_id='parallel-capital',
                domain='capital-order-flow', marker='Independent capital method fixture')
            runner = ResearchBatchRunner(batches_root=root/'batches', cache_root=root/'cache',
                registry=registry, integration_policy=helper.policy())
            profile = replace(helper.profile(), max_concurrency=2, rate_limit_per_minute=1000)
            release, capacity, overflow = (threading.Event() for _ in range(3))
            model, lock = fixtures.FakeModel(), threading.Lock()
            active = peak = 0
            method_starts = set()

            def observe(**event):
                if event['stage'] == 'model_started':
                    with lock:
                        method_starts.add(event['variant_key'])

            def caller(**kwargs):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                    if active == profile.max_concurrency:
                        capacity.set()
                    if active > profile.max_concurrency:
                        overflow.set()
                try:
                    if not release.wait(10):
                        raise AssertionError('fixture calls were not released')
                    return model(**kwargs)
                finally:
                    with lock:
                        active -= 1

            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(runner.run, evidence=full_domain_evidence(root),
                    variants=[helper.main_variant(registry), shadow], profile=profile, secret='',
                    caller=caller, observer=observe)
                try:
                    self.assertTrue(capacity.wait(5))
                    self.assertFalse(overflow.wait(0.3), 'methods each consumed the full profile budget')
                finally:
                    release.set()
                result = future.result(timeout=10)
            self.assertTrue(result['status']['all_variants_completed'], result['status'])
            self.assertEqual(set(result['results']), {'team/main', 'alice/parallel-capital'})
            self.assertEqual(method_starts, {'team/main', 'alice/parallel-capital'})
            self.assertEqual(peak, profile.max_concurrency)


if __name__ == '__main__':
    unittest.main()
