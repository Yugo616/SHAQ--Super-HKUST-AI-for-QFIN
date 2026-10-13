import json
import tempfile
import unittest
from pathlib import Path

import test_research_batch as fixtures
from shaq_daily_oracle.hashing import sha256_payload
from shaq_daily_oracle.research_batch import ResearchBatchRunner, freeze_evidence_bundle, load_frozen_evidence
from shaq_daily_oracle.research_dashboard import ResearchDashboardIndex


class CandidateIsolationTests(unittest.TestCase):
    def test_one_bad_stock_does_not_block_siblings_and_recovery_keeps_result(self):
        self.exercise()

    def test_shared_authentication_failure_does_not_publish_partial_result(self):
        self.exercise(shared=True)

    def test_failed_recovery_keeps_original_results_readable(self):
        self.exercise(failed_recovery=True)

    def test_partially_recovered_stocks_do_not_invalidate_original_results(self):
        self.exercise(partial_recovery=True)

    def test_forged_supplement_report_rejected_even_with_new_self_hash(self):
        self.exercise(tamper=True)

    def test_stock_review_failure_is_isolated(self):
        self.exercise(adversary_failure=True)

    def exercise(self, shared=False, failed_recovery=False, partial_recovery=False, tamper=False, adversary_failure=False):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name); helper=fixtures.ResearchBatchTests(); registry=helper.registry(root)
            stamp='2026-09-04T08:49:00-04:00'
            symbols=('AAPL','MSFT','NVDA')
            records=[dict(evidence_id='ev_market_1',domain='market',provider='fixture',
                source_uri='https://example.test/market',captured_at=stamp,raw_file_path='raw/market.json',
                scope_symbols=['*'],consumer_domains=['market'],root_component_type='market_context')]
            files={'raw/market.json':b'{"SPY":1}'}
            for symbol in symbols:
                records.append(dict(evidence_id='ev_price_'+symbol.lower(),domain='price_volume',provider='fixture',
                    source_uri='https://example.test/'+symbol,captured_at=stamp,raw_file_path='raw/'+symbol+'.json',
                    scope_symbols=[symbol],consumer_domains=['price_volume'],root_component_type='stock_price_volume'))
                files['raw/'+symbol+'.json']=json.dumps({'symbol':symbol}).encode()
            evidence=freeze_evidence_bundle(root=root/'staged',as_of_et=stamp,scheduled_cutoff_et=stamp,
                cutoff_status='on_time',candidates=[{'symbol':s} for s in symbols],records=records,
                files=files,provider_manifest={'profile_id':'fixture'})
            target=root/'evidence'/evidence.manifest['evidence_hash'];target.parent.mkdir()
            evidence.root.replace(target);evidence=load_frozen_evidence(target)
            runner=ResearchBatchRunner(batches_root=root/'batches',cache_root=root/'cache',registry=registry,
                integration_policy=helper.policy())
            model=fixtures.FakeModel()
            failed_symbols={'AAPL','NVDA'} if partial_recovery else {'AAPL'}
            def fail_one(**kw):
                value,audit=model(**kw)
                if adversary_failure and 'REPORTS:\n' in kw['prompt']:
                    if set(json.loads(kw['prompt'].split('REPORTS:\n')[1]))=={'AAPL'}:
                        value['results'][0]['report']['veto']='invalid'
                elif not adversary_failure and 'FROZEN TASKS:' in kw['prompt']:
                    tasks=json.loads(kw['prompt'].split('FROZEN TASKS:\n')[1])
                    if any(t['symbol'] in failed_symbols and t['domain']=='price_volume' for t in tasks):
                        if shared:
                            from shaq_daily_oracle.model_backends import ModelBackendError
                            raise ModelBackendError('forbidden',diagnostic={'kind':'authentication','status':403})
                        value['results'][0]['report']['evidence_ids']=['invented']
                        audit['output_sha256']=sha256_payload(value)
                return value,audit
            first=runner.run(evidence=evidence,variants=[helper.main_variant(registry)],
                profile=helper.profile(),secret='',caller=fail_one)
            if shared:
                self.assertEqual(first['results'],{})
                self.assertIn('team/main',first['status']['failed_variants'])
                return
            self.assertIn('team/main',first['results'],first['status'])
            result=first['results']['team/main']
            self.assertEqual(set(result['excluded_candidates']),failed_symbols)
            self.assertEqual({p['symbol'] for p in result['predictions']},set(symbols)-failed_symbols)
            self.assertFalse(first['status']['all_variants_completed'])
            self.assertIn('team/main',first['status']['incomplete_variants'])
            batch=Path(first['batch_root']);path=next((batch/'variants').glob('*/variant_result.json'))
            original=path.read_bytes();calls=[]
            index=ResearchDashboardIndex(batches_root=root/'batches',database=root/'index.sqlite3')
            self.assertIn('team/main',index.batch_detail(batch.name)['variants'])
            if failed_recovery or partial_recovery:
                def fail_again(**kw):
                    if failed_recovery:
                        from shaq_daily_oracle.model_backends import ModelBackendError
                        raise ModelBackendError('forbidden',diagnostic={'kind':'authentication','status':403})
                    if 'FROZEN TASKS:' in kw['prompt']:
                        tasks=json.loads(kw['prompt'].split('FROZEN TASKS:\n')[1])
                        if any(t['symbol']=='AAPL' for t in tasks): return fail_one(**kw)
                    return model(**kw)
                interrupted=runner.resume(batch_id=batch.name,evidence=evidence,profile=helper.profile(),secret='',caller=fail_again)
                self.assertFalse(interrupted['status']['all_variants_completed'])
                self.assertEqual(interrupted['status']['failed_variants'],{})
                self.assertIn('team/main',index.batch_detail(batch.name)['variants'])
                self.assertEqual(path.read_bytes(),original)
            def recover(**kw):
                calls.append(kw['prompt']);return model(**kw)
            final=runner.resume(batch_id=batch.name,evidence=evidence,profile=helper.profile(),secret='',caller=recover)
            self.assertTrue(final['status']['all_variants_completed'])
            # An independently recovered stock already finished its review in
            # the prior pass; only the still-missing stock is called here.
            self.assertEqual(len(calls),1 if adversary_failure else 2)
            self.assertEqual(path.read_bytes(),original)
            detail=index.batch_detail(batch.name)
            self.assertIn('AAPL',detail['candidate_recoveries']['team/main']['reports_by_symbol'])
            if tamper:
                saved_path=path.parent/'candidate_recovery.json';valid=saved_path.read_bytes()
                saved=json.loads(valid);saved['reports_by_symbol']['AAPL'][0]['thesis']='fabricated'
                saved['recovery_sha256']=sha256_payload({k:v for k,v in saved.items() if k!='recovery_sha256'})
                saved_path.write_text(json.dumps(saved))
                with self.assertRaises(ValueError):index.batch_detail(batch.name)
                saved_path.write_bytes(valid)
            runner.resume(batch_id=batch.name,evidence=evidence,profile=helper.profile(),secret='',
                caller=lambda **kw:self.fail('successful task repeated'))
