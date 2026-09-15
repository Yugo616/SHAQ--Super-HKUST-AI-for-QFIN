import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from shaq_daily_oracle.bundled_versions import install_bundled_versions
from shaq_daily_oracle.hashing import sha256_payload
from shaq_daily_oracle.research_batch import ResearchBatchRunner, VariantSelection, load_frozen_evidence
from shaq_daily_oracle.research_dashboard import ResearchDashboardError, ResearchDashboardIndex
from shaq_daily_oracle.synthesis import synthesis_prompt, validate_synthesis
import test_research_batch as fixtures
from test_research_batch import FakeModel


def decision(ids):
    return {"decisions": [{"symbol": "AAPL", "action": "publish", "direction": "bullish",
        "thesis": "支持", "antithesis": "反例", "resolution": "机制", "comparison": "唯一候选",
        "unknowns": [], "invalidation": ["机制失效"], "evidence_ids": ids}]}


class SynthesisModel(FakeModel):
    def __init__(self, *, valid=False):
        super().__init__()
        self.valid = valid
        self.requests = []
        self.returned = None

    def __call__(self, **kw):
        self.requests.append(kw['prompt'])
        if 'FROZEN SYNTHESIS INPUT:' not in kw['prompt']:
            return super().__call__(**kw)
        self.calls += 1
        result = decision(['ev_price_aapl'] if self.valid else ['invented'])
        audit = {'profile_sha256': kw['profile'].identity(),
            'prompt_sha256': hashlib.sha256(kw['prompt'].encode()).hexdigest(),
            'schema_sha256': sha256_payload(kw['schema']), 'output_sha256': sha256_payload(result)}
        self.returned = (result, audit)
        return result, audit


class PartialBatchIntegrityTests(unittest.TestCase):
    def batch(self, root, observer=None):
        helper = fixtures.ResearchBatchTests()
        registry = helper.registry(root)
        install_bundled_versions(registry)
        staged = helper.evidence(root / 'staged')
        evidence_root = root / 'evidence' / staged.manifest['evidence_hash']
        evidence_root.parent.mkdir()
        staged.root.replace(evidence_root)
        evidence = load_frozen_evidence(evidence_root)
        runner = ResearchBatchRunner(batches_root=root / 'batches', cache_root=root / 'cache',
            registry=registry, integration_policy=helper.policy())
        model = SynthesisModel()
        result = runner.run(evidence=evidence,
            variants=[VariantSelection.from_registry_row(row) for row in registry.list_method_versions()],
            profile=helper.profile(), secret='', caller=model, observer=observer)
        return runner, evidence, result, model

    def test_partial_batch_keeps_good_result_and_authenticates_failed_checkpoints(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            _, _, result, _ = self.batch(root)
            batch = Path(result['batch_root'])
            before = {str(p): p.read_bytes() for p in batch.rglob('*.json')}
            index = ResearchDashboardIndex(batches_root=root / 'batches', database=root / 'index.sqlite3')
            detail = index.batch_detail(batch.name)
            self.assertEqual(set(detail['variants']), {'team/independent-gate-1'})
            self.assertEqual(set(detail['status']['failed_variants']), {'team/cross-domain-synthesis-1'})
            self.assertEqual(detail['status']['orders'], [])
            self.assertGreater(len(detail['model_calls']), len(detail['variants']['team/independent-gate-1']['model_call_audits']))
            self.assertEqual(index.overview()['batches'][0]['source_valid'], 1)
            self.assertEqual(before, {str(p): p.read_bytes() for p in batch.rglob('*.json')})

    def test_fresh_rejected_output_is_preserved_but_never_reused_as_valid_cache(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            runner, evidence, result, model = self.batch(root)
            batch = Path(result['batch_root'])
            rejected = list((batch / 'rejected_model_calls').glob('*.json'))
            self.assertEqual(len(rejected), 1)
            document = json.loads(rejected[0].read_text())
            self.assertEqual(document['result'], model.returned[0])
            self.assertEqual(document['audit'], model.returned[1])
            self.assertEqual(document['validation_error']['kind'], 'unknown_evidence')
            self.assertIn('AAPL', document['validation_error']['message'])
            self.assertIn('FROZEN SYNTHESIS INPUT:', document['prompt'])
            unsigned = {k: v for k, v in document.items() if k != 'rejected_document_sha256'}
            self.assertEqual(document['rejected_document_sha256'], sha256_payload(unsigned))
            self.assertFalse(list((root / 'cache').rglob(document['cache_key'] + '.json')))
            old = {str(p): p.read_bytes() for p in (batch / 'model_calls').glob('*.json')}
            before_good = next((batch / 'variants').glob('*independent-gate*/variant_result.json')).read_bytes()
            repaired = SynthesisModel(valid=True)
            resumed = runner.resume(batch_id=batch.name, evidence=evidence,
                profile=fixtures.ResearchBatchTests().profile(), secret='', caller=repaired)
            self.assertEqual(repaired.calls, 1)
            self.assertTrue(all('FROZEN SYNTHESIS INPUT:' in p for p in repaired.requests))
            self.assertTrue(resumed['status']['all_variants_completed'])
            self.assertEqual(before_good, next((batch / 'variants').glob('*independent-gate*/variant_result.json')).read_bytes())
            self.assertTrue(all(Path(p).read_bytes() == content for p, content in old.items()))
            self.assertEqual(json.loads(rejected[0].read_text()), document)

    def test_extra_self_consistent_foreign_call_is_rejected(self):
        # Self-hashes alone do not prove membership in the failed version's frozen tasks.
        for mutation in ('prompt', 'profile', 'schema', 'missing_success', 'unknown_failed', 'overlap_failed'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as name:
                root = Path(name)
                _, _, result, _ = self.batch(root)
                batch = Path(result['batch_root'])
                calls = list((batch / 'model_calls').glob('*.json'))
                if mutation in {'unknown_failed', 'overlap_failed'}:
                    status = json.loads((batch / 'batch_status.json').read_text())
                    key = 'outsider/unknown' if mutation == 'unknown_failed' else 'team/independent-gate-1'
                    status['failed_variants'][key] = {'message': 'failed'}
                    status['batch_status_sha256'] = sha256_payload({k:v for k,v in status.items() if k != 'batch_status_sha256'})
                    (batch / 'batch_status.json').write_text(json.dumps(status))
                elif mutation == 'missing_success':
                    good = result['results']['team/independent-gate-1']['model_call_audits'][0]['cache_key']
                    (batch / 'model_calls' / (good + '.json')).unlink()
                else:
                    value = json.loads(calls[0].read_text())
                    if mutation == 'prompt':
                        value['prompt'] += '\nUnrelated frozen candidate and evidence'
                        value['key_document']['prompt_sha256'] = hashlib.sha256(value['prompt'].encode()).hexdigest()
                        value['audit']['prompt_sha256'] = value['key_document']['prompt_sha256']
                    elif mutation == 'profile':
                        value['key_document']['profile_sha256'] = '0' * 64
                        value['audit']['profile_sha256'] = '0' * 64
                    else:
                        value['schema']['description'] = 'foreign request'
                        value['key_document']['schema_sha256'] = sha256_payload(value['schema'])
                        value['audit']['schema_sha256'] = value['key_document']['schema_sha256']
                    value['cache_key'] = sha256_payload(value['key_document'])
                    value['audit_sha256'] = sha256_payload(value['audit'])
                    value['cache_document_sha256'] = sha256_payload({k:v for k,v in value.items() if k != 'cache_document_sha256'})
                    (batch / 'model_calls' / (value['cache_key'] + '.json')).write_text(json.dumps(value))
                    status = json.loads((batch / 'batch_status.json').read_text())
                    status['model_call_document_count'] += 1
                    status['batch_status_sha256'] = sha256_payload({k:v for k,v in status.items() if k != 'batch_status_sha256'})
                    (batch / 'batch_status.json').write_text(json.dumps(status))
                index = ResearchDashboardIndex(batches_root=root / 'batches', database=root / 'index.sqlite3')
                with self.assertRaises(ResearchDashboardError):
                    index.batch_detail(batch.name)

    def test_failed_checkpoint_must_obey_full_frozen_schema(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            _, _, result, _ = self.batch(root)
            batch = Path(result['batch_root'])
            good = {row['cache_key'] for row in result['results']['team/independent-gate-1']['model_call_audits']}
            path = next(p for p in (batch / 'model_calls').glob('*.json') if p.stem not in good)
            value = json.loads(path.read_text())
            value['result']['confidence'] = 0.9
            value['audit']['output_sha256'] = sha256_payload(value['result'])
            value['result_sha256'] = sha256_payload(value['result'])
            value['audit_sha256'] = sha256_payload(value['audit'])
            value['cache_document_sha256'] = sha256_payload({k:v for k,v in value.items() if k != 'cache_document_sha256'})
            path.write_text(json.dumps(value))
            index = ResearchDashboardIndex(batches_root=root / 'batches', database=root / 'index.sqlite3')
            with self.assertRaises(ResearchDashboardError):
                index.batch_detail(batch.name)

    def test_citation_errors_distinguish_duplicates_unavailable_and_unknown(self):
        reports = {'AAPL': [{'availability': 'available', 'evidence_ids': ['good']},
            {'availability': 'no_data', 'evidence_ids': ['context']}],
            'MSFT': [{'availability': 'available', 'evidence_ids': ['other']}]}
        roots = {eid: ['r'] for eid in ('good', 'context', 'other')}
        for ids, kind in ((['good', 'good'], 'duplicate_evidence'), (['context'], 'unavailable_evidence'),
                          (['other'], 'unavailable_evidence'), (['invented'], 'unknown_evidence')):
            value = decision(ids)
            other = copy.deepcopy(value['decisions'][0])
            other.update(symbol='MSFT', action='reject', direction='neutral', evidence_ids=[])
            value['decisions'].append(other)
            with self.subTest(ids=ids), self.assertRaises(ValueError) as caught:
                validate_synthesis(value, reports, roots, maximum_predictions=3)
            self.assertEqual(getattr(caught.exception, 'kind', None), kind)
            self.assertIn('AAPL', str(caught.exception))
            self.assertIn(ids[-1], str(caught.exception))

    def test_synthesis_packet_exposes_only_candidate_available_citation_ids(self):
        prompt = synthesis_prompt(reports={'AAPL': [
            {'availability': 'available', 'evidence_ids': ['good', 'good']},
            {'availability': 'no_data', 'evidence_ids': ['context']}]}, adversary={},
            documents={'skills/daily-oracle/SKILL.md': 'method'}, as_of_et='frozen', maximum_predictions=3)
        packet = json.loads(prompt.split('FROZEN SYNTHESIS INPUT:\n')[1])
        self.assertEqual(packet['available_evidence_ids_by_symbol'], {'AAPL': ['good']})
        self.assertEqual(packet['reports_by_symbol']['AAPL'][1]['evidence_ids'], ['context'])

    def test_legacy_partial_batch_resume_keeps_original_synthesis_input_contract(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            runner, evidence, result, _ = self.batch(root)
            batch = Path(result['batch_root'])
            # The old format has the same domain/adversary inputs but no synthesis allowlist.
            manifest = json.loads((batch / 'batch_manifest.json').read_text())
            manifest['batch_identity']['execution_config'].pop('synthesis_citation_contract_version')
            manifest['batch_identity_sha256'] = sha256_payload(manifest['batch_identity'])
            (batch / 'batch_manifest.json').write_text(json.dumps(manifest))
            original_calls = {str(p): p.read_bytes() for p in (batch / 'model_calls').glob('*.json')}
            index = ResearchDashboardIndex(batches_root=root / 'batches', database=root / 'index.sqlite3')
            self.assertEqual(set(index.batch_detail(batch.name)['variants']), {'team/independent-gate-1'})
            repaired = SynthesisModel(valid=True)
            runner.resume(batch_id=batch.name, evidence=evidence, profile=fixtures.ResearchBatchTests().profile(),
                secret='', caller=repaired)
            self.assertEqual(repaired.calls, 1)
            packet = json.loads(repaired.requests[0].split('FROZEN SYNTHESIS INPUT:\n')[1])
            self.assertNotIn('available_evidence_ids_by_symbol', packet)
            self.assertTrue(all(Path(p).read_bytes() == content for p, content in original_calls.items()))
            self.assertEqual(len(index.batch_detail(batch.name)['variants']), 2)

    def test_transport_failure_does_not_preserve_a_nonexistent_model_output(self):
        from shaq_daily_oracle.research_batch import ContentAddressedModelCache
        def failed(**kw):
            raise ValueError('no model response')
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            cache = ContentAddressedModelCache(root / 'cache')
            with self.assertRaisesRegex(ValueError, 'no model response'):
                cache.call(profile=fixtures.ResearchBatchTests().profile(), secret='', prompt='input',
                    schema={}, caller=failed, validate=lambda _: None, snapshot_root=root / 'batch/model_calls')
            self.assertFalse(list(root.rglob('rejected_model_calls/*.json')))
            self.assertEqual(len(list((root / 'batch/call_attempts').glob('*.json'))), 1)

    def test_quota_http_errors_are_not_transient_but_rate_limits_are(self):
        from shaq_daily_oracle.model_backends import ModelBackendError
        from shaq_daily_oracle.model_execution import transient_model_failure
        for code in ('insufficient_quota', 'usage_limit_reached'):
            with self.subTest(code=code):
                self.assertFalse(transient_model_failure(ModelBackendError('HTTP 429', diagnostic={
                    'kind': 'http', 'status': 429, 'provider_code': code})))
        self.assertTrue(transient_model_failure(ModelBackendError('HTTP 429', diagnostic={
            'kind': 'http', 'status': 429, 'provider_code': 'rate_limit_exceeded'})))

    def test_cli_quota_error_has_specific_diagnostic_without_echoing_evidence(self):
        import subprocess
        from dataclasses import replace
        from unittest.mock import patch
        from shaq_daily_oracle.model_backends import ModelBackendError, _codex_cli_call
        from shaq_daily_oracle.model_execution import transient_model_failure
        profile = replace(fixtures.ResearchBatchTests().profile(), protocol='codex-cli')
        raw = "ERROR: You've hit your usage limit. Try again later.\n" + 'FROZEN PRIVATE EVIDENCE ' * 200
        process = subprocess.CompletedProcess(['codex'], 1, '', raw)
        with patch('shaq_daily_oracle.model_backends._local_cli', return_value='/fake/codex.exe'), \
             patch('shaq_daily_oracle.model_backends.run_model_process', return_value=process):
            with self.assertRaises(ModelBackendError) as caught:
                _codex_cli_call(profile=profile, prompt='private evidence', schema={})
        self.assertEqual(caught.exception.diagnostic['kind'], 'quota')
        self.assertNotIn('EVIDENCE', str(caught.exception))
        self.assertLess(len(str(caught.exception)), 150)
        self.assertFalse(transient_model_failure(caught.exception))

    def test_echoed_prompt_cannot_become_public_cli_error(self):
        import subprocess
        from shaq_daily_oracle.model_backends import _local_call_failure
        for text in ('{"thesis":"ERROR: PRIVATE CUSTOMER EVIDENCE 123"}',
                     '{"example":"ERROR: You\'ve hit your usage limit."}'):
            error = _local_call_failure('Codex', subprocess.CompletedProcess([], 1, text, 'connection closed'))
            self.assertEqual(error.diagnostic['kind'], 'local_call')
            self.assertNotIn('PRIVATE', str(error))

    def test_display_task_plan_matches_real_candidates_and_method_stages(self):
        events = []
        with tempfile.TemporaryDirectory() as name:
            self.batch(Path(name), observer=lambda **event: events.append(event))
        plans = {event['variant_key']: event['tasks'] for event in events if event['stage'] == 'tasks_planned'}
        expected_reports = {f'report:AAPL:{domain}' for domain in
            ('capital', 'derivatives', 'event', 'market', 'price_volume', 'relationships')}
        self.assertEqual({task['task_id'] for task in plans['team/independent-gate-1']},
            expected_reports | {'adversary', 'decision'})
        self.assertEqual({task['task_id'] for task in plans['team/cross-domain-synthesis-1']},
            expected_reports | {'adversary', 'synthesis', 'decision'})
        self.assertTrue(any(event['stage'] == 'report_validated' and event.get('status') == 'no_data'
            for event in events))
        self.assertEqual([event['status'] for event in events if event['stage'] == 'synthesis'], ['running'])

    def test_backend_schema_rejection_preserves_returned_output_without_valid_cache(self):
        from dataclasses import replace
        from unittest.mock import patch
        from shaq_daily_oracle.model_backends import ModelBackendError, call_structured
        from shaq_daily_oracle.research_batch import ContentAddressedModelCache
        profile = replace(fixtures.ResearchBatchTests().profile(), protocol='codex-cli')
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            cache = ContentAddressedModelCache(root / 'cache')
            with patch('shaq_daily_oracle.model_backends._codex_cli_call', return_value=({'wrong': 'raw answer'}, {})):
                with self.assertRaises(ModelBackendError):
                    cache.call(profile=profile, secret='', prompt='input', schema={'type': 'object',
                        'required': ['required_field']}, caller=call_structured, snapshot_root=root / 'batch/model_calls')
            rejected = list((root / 'batch/rejected_model_calls').glob('*.json'))
            self.assertEqual(len(rejected), 1)
            document = json.loads(rejected[0].read_text())
            self.assertEqual(document['result'], {'wrong': 'raw answer'})
            self.assertEqual(document['audit']['profile_sha256'], profile.identity())
            self.assertIn('JSON Schema', document['validation_error']['message'])
            self.assertFalse(list((root / 'cache').rglob(document['cache_key'] + '.json')))


if __name__ == '__main__':
    unittest.main()
