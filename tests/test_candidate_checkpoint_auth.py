import copy
import hashlib
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import test_research_batch as fixtures
from test_candidate_execution import full_domain_evidence
from shaq_daily_oracle.bundled_versions import install_bundled_versions
from shaq_daily_oracle.hashing import sha256_payload
from shaq_daily_oracle.model_backends import ModelBackendError
from shaq_daily_oracle.model_execution import ExecutionPolicy
from shaq_daily_oracle.research_batch import ResearchBatchRunner, VariantSelection
from shaq_daily_oracle.research_dashboard import ResearchDashboardError, ResearchDashboardIndex


class CandidateCheckpointAuthenticationTests(unittest.TestCase):
    def test_healthy_review_remains_readable_after_another_candidate_shared_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = fixtures.ResearchBatchTests()
            registry = helper.registry(root)
            evidence = full_domain_evidence(root)
            runner = ResearchBatchRunner(batches_root=root/'batches', cache_root=root/'cache',
                registry=registry, integration_policy=helper.policy())
            profile = replace(helper.profile(), max_concurrency=2, rate_limit_per_minute=1000)
            healthy_review = threading.Event()
            model = fixtures.FakeModel()
            failed_calls = []

            def caller(**kwargs):
                prompt = kwargs['prompt']
                if 'FROZEN TASKS:\n' in prompt:
                    task = json.loads(prompt.split('FROZEN TASKS:\n')[1])[0]
                    if task['symbol'] == 'AAPL' and task['domain'] == 'capital':
                        failed_calls.append(task['symbol'])
                        if not healthy_review.wait(5):
                            raise AssertionError('healthy candidate did not finish its review')
                        raise ModelBackendError('forbidden', diagnostic={'kind': 'authentication', 'status': 403})
                value = model(**kwargs)
                if 'REPORTS:\n' in prompt:
                    healthy_review.set()
                return value

            first = runner.run(evidence=evidence, variants=[helper.main_variant(registry)],
                profile=profile, secret='', caller=caller)
            self.assertEqual(first['results'], {})
            self.assertEqual(set(first['status']['failed_variants']), {'team/main'})
            self.assertEqual(failed_calls, ['AAPL'], 'authentication failure was retried')
            batch = Path(first['batch_root'])
            before = {p: p.read_bytes() for p in batch.rglob('*.json')}
            self.assertEqual(len(list((batch/'model_calls').glob('*.json'))), 7)
            index = ResearchDashboardIndex(batches_root=root/'batches', database=root/'index.sqlite3')
            try:
                detail = index.batch_detail(batch.name)
            except ResearchDashboardError as exc:
                self.fail(f'legitimate completed candidate checkpoint rejected: {exc}')
            self.assertEqual(detail['variants'], {})
            self.assertEqual(len(detail['model_calls']), 7)
            self.assertEqual(index.overview()['batches'][0]['source_valid'], 1)
            self.assertEqual(before, {p: p.read_bytes() for p in batch.rglob('*.json')})

            # Valid self-hashes and a valid review do not authorize a foreign input.
            review = next(json.loads(p.read_text()) for p in (batch/'model_calls').glob('*.json')
                          if 'REPORTS:\n' in json.loads(p.read_text())['prompt'])
            _, audit, _ = runner.cache.call(profile=profile, secret='',
                prompt=review['prompt']+'\n', schema=review['schema'], caller=model,
                snapshot_root=batch/'model_calls')
            status_path = batch/'batch_status.json'
            status_bytes = status_path.read_bytes()
            status = json.loads(status_bytes)
            status['model_call_document_count'] += 1
            status['batch_status_sha256'] = sha256_payload({k: v for k, v in status.items()
                                                          if k != 'batch_status_sha256'})
            status_path.write_text(json.dumps(status))
            with self.assertRaisesRegex(ResearchDashboardError, 'outside frozen failed variants'):
                index.batch_detail(batch.name)
            (batch/'model_calls'/f"{audit['cache_key']}.json").unlink()
            status_path.write_bytes(status_bytes)

            recalled = []

            def recover(**kwargs):
                if 'FROZEN TASKS:\n' in kwargs['prompt']:
                    recalled.extend(t['symbol'] for t in json.loads(kwargs['prompt'].split('FROZEN TASKS:\n')[1]))
                return model(**kwargs)

            resumed = runner.resume(batch_id=batch.name, evidence=evidence, profile=profile,
                secret='', caller=recover)
            self.assertTrue(resumed['status']['all_variants_completed'])
            self.assertEqual(recalled, ['AAPL'] * 6)
            for path, content in before.items():
                if path.parent.name == 'model_calls':
                    self.assertEqual(path.read_bytes(), content)
            self.assertEqual(set(index.batch_detail(batch.name)['variants']), {'team/main'})

    def test_partial_synthesis_checkpoint_is_readable_when_later_decision_fails(self):
        for failure_domain in ('capital', 'adversary'):
            with self.subTest(failure_domain=failure_domain), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                helper = fixtures.ResearchBatchTests()
                registry = helper.registry(root)
                install_bundled_versions(registry)
                variant = next(VariantSelection.from_registry_row(row) for row in registry.list_method_versions()
                               if row['version_id'] == 'cross-domain-synthesis-1')
                runner = ResearchBatchRunner(batches_root=root/'batches', cache_root=root/'cache',
                    registry=registry, integration_policy=helper.policy())
                model = fixtures.FakeModel()
                recovering = False

                def caller(**kwargs):
                    prompt = kwargs['prompt']
                    if 'FROZEN TASKS:\n' in prompt:
                        task = json.loads(prompt.split('FROZEN TASKS:\n')[1])[0]
                        if not recovering and failure_domain == 'capital' and (task['symbol'], task['domain']) == ('AAPL', 'capital'):
                            raise TimeoutError('local candidate failure')
                    if not recovering and failure_domain == 'adversary' and 'REPORTS:\n' in prompt:
                        if set(json.loads(prompt.split('REPORTS:\n')[1])) == {'AAPL'}:
                            raise TimeoutError('local review failure')
                    if 'FROZEN SYNTHESIS INPUT:\n' not in prompt:
                        return model(**kwargs)
                    packet = json.loads(prompt.split('FROZEN SYNTHESIS INPUT:\n')[1])
                    self.assertEqual(set(packet['reports_by_symbol']), {'AAPL', 'MSFT'} if recovering else {'MSFT'})
                    value = {'decisions': [{'symbol': symbol, 'action': 'reject', 'direction': 'neutral',
                        'thesis': '观察', 'antithesis': '反例', 'resolution': '方向不明', 'comparison': '唯一完整候选',
                        'unknowns': [], 'invalidation': [], 'evidence_ids': []}
                        for symbol in sorted(packet['reports_by_symbol'])]}
                    return value, {'profile_sha256': kwargs['profile'].identity(),
                        'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
                        'schema_sha256': sha256_payload(kwargs['schema']), 'output_sha256': sha256_payload(value)}

                with patch('shaq_daily_oracle.research_batch.execute_decision_script',
                           side_effect=ValueError('fixture downstream decision failed')):
                    evidence = full_domain_evidence(root)
                    profile = replace(helper.profile(), max_concurrency=2, rate_limit_per_minute=1000)
                    first = runner.run(evidence=evidence, variants=[variant], profile=profile,
                        secret='', caller=caller,
                        execution_policy=ExecutionPolicy(transient_retries=0, citation_retries=0))
                self.assertEqual(first['results'], {})
                self.assertIn('fixture downstream decision failed',
                    first['status']['failed_variants']['team/cross-domain-synthesis-1']['message'])
                batch = Path(first['batch_root'])
                before = {p: p.read_bytes() for p in batch.rglob('*.json')}
                expected = 8 if failure_domain == 'capital' else 14
                self.assertEqual(len(list((batch/'model_calls').glob('*.json'))), expected)
                index = ResearchDashboardIndex(batches_root=root/'batches', database=root/'index.sqlite3')
                try:
                    detail = index.batch_detail(batch.name)
                except ResearchDashboardError as exc:
                    self.fail(f'legitimate partial synthesis checkpoint rejected: {exc}')
                self.assertEqual(len(detail['model_calls']), expected)
                self.assertEqual(before, {p: p.read_bytes() for p in batch.rglob('*.json')})
                recovering = True
                resumed = runner.resume(batch_id=batch.name, evidence=evidence, profile=profile,
                    secret='', caller=caller)
                self.assertTrue(resumed['status']['all_variants_completed'])
                try:
                    detail = index.batch_detail(batch.name)
                except ResearchDashboardError as exc:
                    self.fail(f'legitimate earlier candidate subset rejected after recovery: {exc}')
                self.assertEqual(set(detail['variants']), {'team/cross-domain-synthesis-1'})
                self.assertEqual(len(detail['model_calls']), 16)
                self.assertEqual(index.overview()['batches'][0]['source_valid'], 1)
                for path, content in before.items():
                    if path.parent.name == 'model_calls':
                        self.assertEqual(path.read_bytes(), content)
                historical_synthesis = next(json.loads(content) for path, content in before.items()
                    if path.parent.name == 'model_calls' and 'FROZEN SYNTHESIS INPUT:\n' in json.loads(content)['prompt'])
                for mutation in ('prompt', 'schema'):
                    with self.subTest(foreign_checkpoint=mutation):
                        foreign = copy.deepcopy(historical_synthesis)
                        if mutation == 'prompt':
                            foreign['prompt'] += '\n'
                        else:
                            foreign['schema']['properties']['decisions']['items']['properties']['symbol']['enum'] = ['AAPL']
                        foreign['key_document']['prompt_sha256'] = hashlib.sha256(foreign['prompt'].encode()).hexdigest()
                        foreign['key_document']['schema_sha256'] = sha256_payload(foreign['schema'])
                        foreign['cache_key'] = sha256_payload(foreign['key_document'])
                        foreign['audit']['prompt_sha256'] = foreign['key_document']['prompt_sha256']
                        foreign['audit']['schema_sha256'] = foreign['key_document']['schema_sha256']
                        foreign['audit_sha256'] = sha256_payload(foreign['audit'])
                        foreign['cache_document_sha256'] = sha256_payload({k: v for k, v in foreign.items()
                                                                         if k != 'cache_document_sha256'})
                        foreign_path = batch/'model_calls'/f"{foreign['cache_key']}.json"
                        foreign_path.write_text(json.dumps(foreign))
                        status_path = batch/'batch_status.json'
                        status_bytes = status_path.read_bytes()
                        status = json.loads(status_bytes)
                        status['model_call_document_count'] += 1
                        status['batch_status_sha256'] = sha256_payload({k: v for k, v in status.items()
                                                                       if k != 'batch_status_sha256'})
                        status_path.write_text(json.dumps(status))
                        try:
                            with self.assertRaisesRegex(ResearchDashboardError, 'outside frozen failed variants'):
                                index.batch_detail(batch.name)
                        finally:
                            foreign_path.unlink()
                            status_path.write_bytes(status_bytes)
                attempts = {p: p.read_bytes() for p in (batch/'attempts').glob('*.json')}
                for mutation in ('batch', 'method', 'filename', 'status_hash'):
                    with self.subTest(invalid_attempt=mutation):
                        for path in attempts:
                            path.unlink()
                        invalid = copy.deepcopy(first['status'])
                        if mutation == 'batch':
                            invalid['batch_id'] = 'LAB-other-frozen-batch'
                        elif mutation == 'method':
                            invalid['failed_variants'] = {'foreign/unknown': {'message': 'failed'}}
                        invalid['batch_status_sha256'] = sha256_payload({k: v for k, v in invalid.items()
                                                                       if k != 'batch_status_sha256'})
                        if mutation == 'status_hash':
                            invalid['batch_status_sha256'] = '0' * 64
                        filename = '0' * 64 if mutation == 'filename' else sha256_payload(invalid)
                        path = batch/'attempts'/f'{filename}.json'
                        path.write_text(json.dumps(invalid))
                        try:
                            with self.assertRaisesRegex(ResearchDashboardError, 'outside frozen failed variants'):
                                index.batch_detail(batch.name)
                        finally:
                            path.unlink()
                            for original, content in attempts.items():
                                original.write_bytes(content)
                noise = batch/'attempts'/('0' * 64 + '.json')
                noise.write_text('{}')
                (batch/'attempts'/f'{sha256_payload([1])}.json').write_text('[1]')
                self.assertEqual(set(index.batch_detail(batch.name)['variants']), {'team/cross-domain-synthesis-1'})


if __name__ == '__main__':
    unittest.main()
