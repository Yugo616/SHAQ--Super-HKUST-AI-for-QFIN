import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import test_research_batch as fixtures
from shaq_daily_oracle.hashing import sha256_payload
from shaq_daily_oracle.research_batch import ContentAddressedModelCache, ResearchBatchError, _run_domain
from shaq_daily_oracle.research_dashboard import _verified_model_call


class CitationRecoveryTests(unittest.TestCase):
    def setup_run(self, root):
        helper = fixtures.ResearchBatchTests()
        registry = helper.registry(root)
        variant = helper.main_variant(registry)
        return dict(domain='price_volume', evidence=helper.evidence(root),
                    documents=registry.effective_skills(variant.version_id, variant.author),
                    profile=helper.profile(), secret='', cache=ContentAddressedModelCache(root/'cache'),
                    snapshot_root=root/'batch/model_calls', prompt_format_version=2)

    def test_missing_characters_are_regenerated_not_guessed_and_success_is_reused(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); args = self.setup_run(root); requests = []; events = []
            model = fixtures.FakeModel()
            def caller(**kw):
                requests.append(kw['prompt'])
                value, audit = model(**kw)
                if len(requests) == 1:
                    value['results'][0]['report']['evidence_ids'] = ['ev_price_apl']
                    audit['output_sha256'] = sha256_payload(value)
                return value, audit
            reports, audits = _run_domain(**args, caller=caller, observer=lambda **e: events.append(e))
            self.assertEqual(len(requests), 2)
            self.assertNotEqual(requests[0], requests[1])
            self.assertTrue(requests[1].endswith(requests[0]))
            self.assertEqual(reports[0]['evidence_ids'], ['ev_price_aapl'])
            self.assertTrue(any(e['stage']=='model_retry_scheduled' for e in events))
            rejected = list((root/'batch/rejected_model_calls').glob('*.json'))
            self.assertEqual(len(rejected), 1)
            self.assertEqual(json.loads(rejected[0].read_text())['result']['results'][0]['report']['evidence_ids'], ['ev_price_apl'])
            saved = list((root/'batch/model_calls').glob('*.json'))
            document = _verified_model_call(saved[0])
            self.assertEqual(document['audit']['prompt_sha256'], hashlib.sha256(requests[1].encode()).hexdigest())
            _run_domain(**args, caller=lambda **kw:self.fail('successful call repeated'))

    def test_repeated_bad_citation_stops_after_one_correction_without_valid_checkpoint(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name); args=self.setup_run(root); calls=[]; model=fixtures.FakeModel()
            def caller(**kw):
                calls.append(kw)
                result,audit=model(**kw)
                result['results'][0]['report']['evidence_ids']=['invented']
                audit['output_sha256']=sha256_payload(result)
                return result,audit
            with self.assertRaises(ResearchBatchError):
                _run_domain(**args,caller=caller)
            self.assertEqual(len(calls),2)
            self.assertFalse(list((root/'batch/model_calls').glob('*.json')))
            self.assertEqual(len(list((root/'batch/rejected_model_calls').glob('*.json'))),2)

    def test_duplicate_citations_regenerated_and_new_schema_limits_ids(self):
        with tempfile.TemporaryDirectory() as name:
            args=self.setup_run(Path(name)); args['prompt_format_version']=3
            calls=[]; model=fixtures.FakeModel()
            def caller(**kw):
                calls.append(kw)
                result,audit=model(**kw)
                if len(calls)==1:
                    result['results'][0]['report']['evidence_ids']*=2
                    audit['output_sha256']=sha256_payload(result)
                return result,audit
            reports,_=_run_domain(**args,caller=caller)
            self.assertEqual(len(calls),2)
            self.assertEqual(reports[0]['evidence_ids'],['ev_price_aapl'])
            variants=calls[0]['schema']['properties']['results']['items']['properties']['report']['anyOf']
            for variant in variants:
                self.assertEqual(set(variant['properties']['evidence_ids']['items']['enum']),{'ev_market_1','ev_price_aapl'})

    def test_auth_error_is_not_retried(self):
        from shaq_daily_oracle.model_backends import ModelBackendError
        with tempfile.TemporaryDirectory() as name:
            args=self.setup_run(Path(name)); calls=[]
            def caller(**kw):
                calls.append(kw)
                raise ModelBackendError('forbidden',diagnostic={'status':403})
            with self.assertRaises(ResearchBatchError):_run_domain(**args,caller=caller)
            self.assertEqual(len(calls),1)

    def test_backend_enum_rejection_is_recovered_with_original_output_retained(self):
        from shaq_daily_oracle.model_backends import ModelBackendError
        with tempfile.TemporaryDirectory() as name:
            args=self.setup_run(Path(name)); calls=[]; model=fixtures.FakeModel()
            def caller(**kw):
                calls.append(kw)
                value,audit=model(**kw)
                if len(calls)==1:
                    value['results'][0]['report']['evidence_ids']=['ev_price_apl']
                    error=ModelBackendError('schema validation failed', diagnostic={'kind':'output_schema'})
                    audit['output_sha256']=sha256_payload(value)
                    error.rejected_output={'result':value,'audit':audit}
                    raise error
                return value,audit
            reports,_=_run_domain(**args,caller=caller)
            self.assertEqual(len(calls),2)
            self.assertEqual(reports[0]['evidence_ids'],['ev_price_aapl'])

    def test_wrong_model_with_bad_reference_must_not_trigger_correction(self):
        from unittest.mock import patch
        from shaq_daily_oracle.model_backends import call_structured
        with tempfile.TemporaryDirectory() as name:
            root=Path(name); args=self.setup_run(root); calls=[]; model=fixtures.FakeModel()
            def transport(**kw):
                calls.append(kw)
                value,audit=model(**kw)
                value['results'][0]['report']['evidence_ids']=['invented']
                return value,{'response_model':'WRONG-MODEL'}
            with patch('shaq_daily_oracle.model_backends._openai_chat_call',side_effect=transport):
                with self.assertRaises(ResearchBatchError):
                    _run_domain(**args,caller=call_structured)
            self.assertEqual(len(calls),1)
            rejected=json.loads(next((root/'batch/rejected_model_calls').glob('*.json')).read_text())
            self.assertIn('different model',rejected['validation_error']['message'])
            self.assertFalse(list((root/'batch/model_calls').glob('*.json')))

    def test_corrective_request_preserved_when_transport_never_returns(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name); args=self.setup_run(root); calls=[]; model=fixtures.FakeModel()
            def caller(**kw):
                calls.append(kw['prompt'])
                if len(calls)>1: raise TimeoutError('temporary timeout')
                value,audit=model(**kw)
                value['results'][0]['report']['evidence_ids']=['invented']
                return value,audit
            with self.assertRaises(ResearchBatchError):_run_domain(**args,caller=caller)
            self.assertEqual(len(calls),3)
            saved=json.loads(next((root/'batch/call_attempts').glob('*.json')).read_text())
            for attempt,request in zip(saved['attempts'],calls):
                self.assertEqual(attempt['request_prompt'],request)
                self.assertEqual(attempt['request_prompt_sha256'],hashlib.sha256(request.encode()).hexdigest())

    def test_real_backend_schema_rejection_has_explicit_category(self):
        from shaq_daily_oracle.model_backends import _validate_result,ModelBackendError
        with self.assertRaises(ModelBackendError) as raised:
            _validate_result({'id':'bad'},{'type':'object','properties':{'id':{'enum':['good']}}})
        self.assertEqual(raised.exception.diagnostic['kind'],'output_schema')

    def test_tampered_corrective_prompt_rejected_even_with_recomputed_self_hash(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name); args=self.setup_run(root); calls=[]; model=fixtures.FakeModel()
            def caller(**kw):
                calls.append(kw)
                value,audit=model(**kw)
                if len(calls)==1:
                    value['results'][0]['report']['evidence_ids']=['invented']
                    audit['output_sha256']=sha256_payload(value)
                return value,audit
            _run_domain(**args,caller=caller)
            path=next((root/'batch/model_calls').glob('*.json'))
            document=json.loads(path.read_text())
            document['request_prompt']='Added future prices\n'+document['prompt']
            document['audit']['prompt_sha256']=hashlib.sha256(document['request_prompt'].encode()).hexdigest()
            document['audit_sha256']=sha256_payload(document['audit'])
            document['cache_document_sha256']=sha256_payload({k:v for k,v in document.items() if k!='cache_document_sha256'})
            path.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError,'corrective request'):_verified_model_call(path)

    def test_other_task_reference_never_accepted_even_if_in_group_schema(self):
        from shaq_daily_oracle.research_batch import freeze_evidence_bundle
        with tempfile.TemporaryDirectory() as name:
            root=Path(name); args=self.setup_run(root); source=args['evidence']
            stamp=source.manifest['as_of_et']
            records=[]; files={}
            for symbol in ('AAPL','MSFT'):
                records.append(dict(evidence_id='ev_price_'+symbol.lower(),domain='price_volume',
                    provider='fixture',source_uri='https://example.test/'+symbol,captured_at=stamp,
                    raw_file_path='raw/'+symbol+'.json',scope_symbols=[symbol],consumer_domains=['price_volume']))
                files['raw/'+symbol+'.json']=json.dumps({'symbol':symbol}).encode()
            args['evidence']=freeze_evidence_bundle(root=root/'second-evidence',as_of_et=stamp,
                scheduled_cutoff_et=source.manifest['scheduled_cutoff_et'],cutoff_status='on_time',
                candidates=[{'symbol':s} for s in ('AAPL','MSFT')],records=records,files=files,
                provider_manifest={'profile_id':'fixture'})
            # Legacy grouped tasks must remain grouped when recovering an old batch.
            args['prompt_format_version']=1
            calls=[]; model=fixtures.FakeModel()
            def caller(**kw):
                calls.append(kw); result,audit=model(**kw)
                result['results'][0]['report']['evidence_ids']=['ev_price_msft']
                audit['output_sha256']=sha256_payload(result)
                return result,audit
            with self.assertRaises(ResearchBatchError):_run_domain(**args,caller=caller)
            self.assertEqual(len(calls),2)
            self.assertFalse(list((root/'batch/model_calls').glob('*.json')))

    def test_partial_batch_recovery_keeps_successful_calls_and_history_readable(self):
        from shaq_daily_oracle.research_batch import ResearchBatchRunner, load_frozen_evidence
        from shaq_daily_oracle.research_dashboard import ResearchDashboardIndex
        with tempfile.TemporaryDirectory() as name:
            root=Path(name); helper=fixtures.ResearchBatchTests(); registry=helper.registry(root)
            runner=ResearchBatchRunner(batches_root=root/'batches',cache_root=root/'cache',
                registry=registry,integration_policy=helper.policy())
            staged=helper.evidence(root/'staged')
            target=root/'evidence'/staged.manifest['evidence_hash']; target.parent.mkdir()
            staged.root.replace(target); evidence=load_frozen_evidence(target)
            model=fixtures.FakeModel(); requests=[]
            def bad(**kw):
                result,audit=model(**kw)
                if 'FROZEN TASKS:' in kw['prompt']:
                    tasks=json.loads(kw['prompt'].split('FROZEN TASKS:\n')[1])
                    if tasks[0]['domain']=='price_volume':
                        result['results'][0]['report']['evidence_ids']=['invented']
                        audit['output_sha256']=sha256_payload(result)
                return result,audit
            result=runner.run(evidence=evidence,variants=[helper.main_variant(registry)],
                profile=helper.profile(),secret='',caller=bad)
            self.assertFalse(result['status']['all_variants_completed'])
            batch=Path(result['batch_root'])
            saved={p:p.read_bytes() for p in (batch/'model_calls').glob('*.json')}
            self.assertTrue(saved)
            def corrected(**kw):
                requests.append(kw['prompt'])
                value,audit=model(**kw)
                if len(requests)==1:
                    value['results'][0]['report']['evidence_ids']=['ev_price_apl']
                    audit['output_sha256']=sha256_payload(value)
                return value,audit
            completed=runner.resume(batch_id=batch.name,evidence=evidence,
                profile=helper.profile(),secret='',caller=corrected)
            self.assertTrue(completed['status']['all_variants_completed'])
            self.assertEqual(len(requests),3)  # bad price, corrected price, adversary
            self.assertTrue(all(p.read_bytes()==v for p,v in saved.items()))
            index=ResearchDashboardIndex(batches_root=root/'batches',database=root/'index.sqlite3')
            detail=index.batch_detail(batch.name)
            self.assertEqual(set(detail['variants']),{'team/main'})
