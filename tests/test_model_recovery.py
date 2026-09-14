from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from shaq_daily_oracle.model_backends import ModelBackendError, ModelProfile
from shaq_daily_oracle.research_batch import ContentAddressedModelCache
from shaq_daily_oracle.hashing import sha256_payload
import test_research_batch as fixtures
FakeModel = fixtures.FakeModel


class ExecutionRecoveryTests(unittest.TestCase):
    def test_new_model_view_preserves_existing_semantics_and_reduces_repetitive_packet(self):
        from shaq_daily_oracle.research_batch import _tasks_for_domain, _domain_prompt
        from shaq_daily_oracle.model_execution import expand_market_tables
        helper=fixtures.ResearchBatchTests()
        bars=[{'timestamp':f'2025-{1+i//28:02d}-{1+i%28:02d}','open':100.12,
            'high':102.13,'low':99.22,'close':101.31,'volume':123456,'source':'fixture'} for i in range(240)]
        benchmark={'daily_and_premarket':{symbol:{'daily':{'bars':bars}} for symbol in ['SPY','QQQ','DIA','IWM']}}
        stock={'symbol':'AAPL','daily':{'bars':bars}}
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);evidence=helper.evidence(root);registry=helper.registry(root)
            with patch('shaq_daily_oracle.research_batch._evidence_content',
                    side_effect=lambda path,digest:benchmark if path.endswith('market.json') else stock):
                old=_tasks_for_domain(evidence,'price_volume',prompt_format_version=1)
                new=_tasks_for_domain(evidence,'price_volume',prompt_format_version=2)
            docs=registry.effective_skills('main','team')
            old_prompt=_domain_prompt(domain='price_volume',tasks=old,documents=docs,prompt_format_version=1)
            new_prompt=_domain_prompt(domain='price_volume',tasks=new,documents=docs,prompt_format_version=2)
            self.assertLess(len(new_prompt.encode()),len(old_prompt.encode()))
            old_pool=json.loads(old_prompt.split('SHARED EVIDENCE:\n')[1].split('\n\nFROZEN TASKS:')[0])
            new_pool=json.loads(new_prompt.split('SHARED EVIDENCE:\n')[1].split('\n\nFROZEN TASKS:')[0])
            self.assertEqual({key:expand_market_tables(value) for key,value in new_pool.items()},old_pool)

    def test_execution_settings_change_without_changing_saved_model(self):
        import test_research_lab_foundation as foundation
        from shaq_daily_oracle.research_settings import ResearchSettingsStore
        with tempfile.TemporaryDirectory() as name:
            store=ResearchSettingsStore(foundation.ResearchLabFoundationTests().paths(Path(name)))
            original=fixtures.ResearchBatchTests().profile().public_dict()
            store._save({**store.load(),'model_profiles':[original]})
            store.save_execution_policy({'timeout_seconds':900,'transient_retries':0})
            self.assertEqual(store.load()['model_profiles'],[original])
            self.assertEqual(store.execution_policy().timeout_seconds,900)
            with self.assertRaises(ValueError):
                store.save_execution_policy({'timeout_seconds':0,'transient_retries':1})

    def test_windows_tree_job_is_owned_and_windowless(self):
        from unittest.mock import MagicMock
        from shaq_daily_oracle.model_execution import run_model_process
        process=MagicMock();process.pid=4567
        process.communicate.side_effect=[subprocess.TimeoutExpired('worker',1),('', '')]
        owner=MagicMock()
        with patch('shaq_daily_oracle.model_execution.sys.platform','win32'), \
             patch('shaq_daily_oracle.model_execution.subprocess.Popen',return_value=process) as popen, \
             patch('shaq_daily_oracle.model_execution.WindowsProcessJob',return_value=owner):
            with self.assertRaises(subprocess.TimeoutExpired):
                run_model_process(['worker.exe'],timeout=1,capture_output=True)
            self.assertTrue(popen.call_args.kwargs['creationflags'] & 0x08000000)
            self.assertTrue(popen.call_args.kwargs['creationflags'] & 0x4)
            self.assertNotIn('start_new_session',popen.call_args.kwargs)
            owner.assign_and_resume.assert_called_once_with(process)
            owner.terminate.assert_called_once_with(timeout=1)

    def test_timeout_retries_once_and_policy_does_not_change_cache_identity(self):
        from shaq_daily_oracle.model_execution import ExecutionPolicy
        with tempfile.TemporaryDirectory() as name:
            profile = fixtures.ResearchBatchTests().profile()
            cache = ContentAddressedModelCache(Path(name))
            calls = []
            def caller(**kwargs):
                calls.append(kwargs['profile'].identity())
                if len(calls) == 1:
                    raise ModelBackendError('timeout', diagnostic={'kind': 'timeout'})
                return {'ok': True}, {}
            result, audit, hit = cache.call(profile=profile, secret='', prompt='test', schema={},
                caller=caller, execution_policy=ExecutionPolicy(timeout_seconds=601))
            self.assertEqual(result, {'ok': True})
            self.assertEqual(calls, [profile.identity(), profile.identity()])
            self.assertEqual(audit['attempt_count'], 2)
            self.assertEqual(audit['input_bytes'], 4)
            self.assertGreaterEqual(audit['elapsed_seconds'], 0)
            self.assertFalse(hit)
            _, _, hit = cache.call(profile=profile, secret='', prompt='test', schema={},
                caller=lambda **kw: self.fail('valid cache recalled'),
                execution_policy=ExecutionPolicy(timeout_seconds=900, transient_retries=0))
            self.assertTrue(hit)

    def test_authentication_and_schema_errors_are_not_retried(self):
        from shaq_daily_oracle.model_execution import ExecutionPolicy
        for error in [ModelBackendError('forbidden', diagnostic={'kind':'http','status':403}),
                      ModelBackendError('JSON Schema validation failed')]:
            with self.subTest(error=str(error)), tempfile.TemporaryDirectory() as name:
                calls=[]
                def caller(**kwargs):
                    calls.append(1)
                    raise error
                with self.assertRaises(ModelBackendError):
                    ContentAddressedModelCache(Path(name)).call(profile=fixtures.ResearchBatchTests().profile(),
                        secret='',prompt='test',schema={},caller=caller,execution_policy=ExecutionPolicy())
                self.assertEqual(len(calls),1)

    def test_compact_tables_roundtrip_missing_null_and_all_ohlc(self):
        from shaq_daily_oracle.model_execution import compact_market_tables, expand_market_tables
        raw={'daily_and_premarket': {'SPY': {'daily': {'bars': [
            {'timestamp':'2026-09-01','open':1,'high':3,'low':0,'close':2,'volume':0},
            {'timestamp':'2026-09-02','open':None,'close':2,'source':'a'}, {}]}}}}
        table=compact_market_tables(raw)
        self.assertEqual(expand_market_tables(table),raw)
        self.assertEqual(raw['daily_and_premarket']['SPY']['daily']['bars'][1]['open'],None)
        self.assertNotEqual(table,raw)

    def test_timeout_terminates_only_own_process_tree(self):
        from shaq_daily_oracle.model_execution import run_model_process
        from shaq_daily_oracle.background_process import background_process_options
        with tempfile.TemporaryDirectory() as name:
            pidfile=Path(name)/'pid'
            unrelated=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'], **background_process_options())
            script=('import subprocess,sys,time; '
                'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"],'
                'creationflags=0x08000000 if sys.platform=="win32" else 0); '
                'open(sys.argv[1],"w").write(str(p.pid)); time.sleep(30)')
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    run_model_process([sys.executable,'-c',script,str(pidfile)],timeout=0.5,
                        text=True,capture_output=True)
                self.assertIsNone(unrelated.poll())
                child=int(pidfile.read_text())
                if sys.platform == 'win32':
                    import ctypes
                    from ctypes import wintypes
                    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
                    kernel.OpenProcess.restype=wintypes.HANDLE
                    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
                    kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
                    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
                    handle=kernel.OpenProcess(0x1000,False,child)
                    if handle:
                        try:
                            code=wintypes.DWORD()
                            self.assertTrue(kernel.GetExitCodeProcess(handle,ctypes.byref(code)))
                            self.assertNotEqual(code.value,259,'owned child still active')
                        finally:kernel.CloseHandle(handle)
                    else:self.assertEqual(ctypes.get_last_error(),87)
                else:
                    for _ in range(30):
                        state=subprocess.run(['ps','-o','stat=','-p',str(child)],capture_output=True,text=True).stdout.strip()
                        if not state or state.startswith('Z'):break
                        time.sleep(.05)
                    self.assertTrue(not state or state.startswith('Z'),state)
            finally:
                unrelated.terminate();unrelated.wait()


class BatchRecoveryTests(unittest.TestCase):
    def test_service_resume_uses_original_evidence_without_today_collection(self):
        import shutil
        from dataclasses import replace
        import test_research_lab_foundation as foundation
        from shaq_daily_oracle.lab_service import LabService
        from shaq_daily_oracle.research_batch import ResearchBatchRunner
        helper=fixtures.ResearchBatchTests()
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);service=LabService(foundation.ResearchLabFoundationTests().paths(root))
            source=helper.evidence(root/'fixture')
            target=service.paths.research_root/'evidence'/source.manifest['evidence_hash']
            shutil.copytree(source.root,target)
            from shaq_daily_oracle.research_batch import load_frozen_evidence
            evidence=load_frozen_evidence(target)
            profile=replace(helper.profile(),protocol='codex-cli')
            service.settings._save({**service.settings.load(),'model_profiles':[profile.public_dict()]})
            runner=ResearchBatchRunner(batches_root=service.paths.batches_root,
                cache_root=service.paths.research_root/'cache/model_calls',registry=service.registry,integration_policy=helper.policy())
            first=runner.run(evidence=evidence,variants=[helper.main_variant(service.registry)],profile=profile,secret='',
                caller=lambda **kw: (_ for _ in ()).throw(ValueError('stop fixture')))
            with patch.object(service,'_today_evidence',side_effect=AssertionError('recollected evidence')), \
                 patch.object(service,'start_result_refresh',return_value={'status':'not_due'}), \
                 patch('shaq_daily_oracle.lab_service.start_guarded_thread',side_effect=lambda paths,thread:thread.run()), \
                 patch('shaq_daily_oracle.model_backends._codex_cli_call',side_effect=lambda **kw:FakeModel()(**kw,secret='')):
                job=service.resume_batch(first['status']['batch_id'])
            self.assertEqual(job['status'],'complete',job)
            self.assertEqual(job['batch_id'],first['status']['batch_id'])

    def test_late_resume_preserves_original_on_time_method_score(self):
        from datetime import datetime
        from shaq_daily_oracle.research_batch import ResearchBatchRunner
        class BeforeDeadline(datetime):
            @classmethod
            def now(cls,tz=None):return cls.fromisoformat('2026-09-04T08:55:00-04:00')
        class AfterDeadline(datetime):
            @classmethod
            def now(cls,tz=None):return cls.fromisoformat('2026-09-04T09:01:00-04:00')
        helper=fixtures.ResearchBatchTests()
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);registry=helper.registry(root);evidence=helper.evidence(root)
            shadow=helper.install_shadow(registry,version_id='timeout-shadow',domain='market-common-shock',marker='FAIL_ONLY_SHADOW')
            runner=ResearchBatchRunner(batches_root=root/'batches',cache_root=root/'cache',registry=registry,integration_policy=helper.policy())
            attempts=[]
            def initial(**kwargs):
                if 'FAIL_ONLY_SHADOW' in kwargs['prompt']:
                    attempts.append(1);raise TimeoutError('temporary')
                return FakeModel()(**kwargs)
            with patch('shaq_daily_oracle.research_batch.datetime',BeforeDeadline):
                first=runner.run(evidence=evidence,variants=[helper.main_variant(registry),shadow],profile=helper.profile(),secret='',caller=initial)
            self.assertEqual(len(attempts),2)
            self.assertTrue(first['results']['team/main']['score_eligible'])
            with patch('shaq_daily_oracle.research_batch.datetime',AfterDeadline):
                done=runner.resume(batch_id=first['status']['batch_id'],evidence=evidence,profile=helper.profile(),secret='',caller=FakeModel())
            self.assertEqual(done['results']['team/main'],first['results']['team/main'])
            self.assertFalse(done['results']['alice/timeout-shadow']['score_eligible'])
            self.assertEqual(done['results']['alice/timeout-shadow']['completed_at_et'],'2026-09-04T09:01:00-04:00')

    def test_only_failed_single_stock_group_is_recalled_and_format_is_versioned(self):
        from shaq_daily_oracle.research_batch import ResearchBatchRunner, freeze_evidence_bundle, _domain_prompt
        helper=fixtures.ResearchBatchTests()
        with tempfile.TemporaryDirectory() as name:
            root=Path(name); registry=helper.registry(root); source=helper.evidence(root)
            records=[{**r,'scope_symbols':['*']} for r in source.manifest['records']]
            evidence=freeze_evidence_bundle(root=root/'two', as_of_et=source.manifest['as_of_et'],
                scheduled_cutoff_et=source.manifest['scheduled_cutoff_et'],cutoff_status='on_time',
                candidates=[source.candidate_intake['candidates'][0],
                    {**source.candidate_intake['candidates'][0],'symbol':'MSFT'}], records=records,
                files={p:(source.root/p).read_bytes() for p in source.manifest['file_sha256']},
                provider_manifest=source.manifest['provider_manifest'])
            runner=ResearchBatchRunner(batches_root=root/'batches',cache_root=root/'cache',
                registry=registry,integration_policy=helper.policy())
            fake=FakeModel();failed=[]
            def initial(**kwargs):
                if 'FROZEN TASKS:\n' in kwargs['prompt']:
                    tasks=json.loads(kwargs['prompt'].split('FROZEN TASKS:\n')[1])
                    if tasks[0]['domain']=='price_volume':
                        self.assertEqual(len(tasks),1)
                        if tasks[0]['symbol']=='AAPL':
                            failed.append('AAPL');raise TimeoutError('call expired')
                return fake(**kwargs)
            result=runner.run(evidence=evidence,variants=[helper.main_variant(registry)],
                profile=helper.profile(),secret='',caller=initial)
            self.assertEqual(failed,['AAPL','AAPL'])
            self.assertEqual(len(list((Path(result['batch_root'])/'model_calls').glob('*.json'))),2)
            recalled=[]
            def resumed(**kwargs):
                if 'FROZEN TASKS:\n' in kwargs['prompt']:
                    tasks=json.loads(kwargs['prompt'].split('FROZEN TASKS:\n')[1])
                    recalled.extend((task['domain'],task['symbol']) for task in tasks)
                return fake(**kwargs)
            done=runner.resume(batch_id=result['status']['batch_id'],evidence=evidence,
                profile=helper.profile(),secret='',caller=resumed)
            self.assertTrue(done['status']['all_variants_completed'])
            self.assertEqual(recalled,[('price_volume','AAPL')])
            docs=registry.effective_skills('main','team')
            self.assertNotEqual(_domain_prompt(domain='market',tasks=[],documents=docs,prompt_format_version=1),
                _domain_prompt(domain='market',tasks=[],documents=docs,prompt_format_version=2))

    def test_legacy_format_and_cache_resume_without_current_method_lookup(self):
        import shutil
        from shaq_daily_oracle.research_batch import ResearchBatchRunner, _atomic_json, _domain_prompt, _tasks_for_domain
        from shaq_daily_oracle.sandboxed_codex import _report_schema
        helper=fixtures.ResearchBatchTests()
        with tempfile.TemporaryDirectory() as name:
            root=Path(name); registry=helper.registry(root); evidence=helper.evidence(root)
            runner=ResearchBatchRunner(batches_root=root/'batches',cache_root=root/'cache',
                registry=registry,integration_policy=helper.policy())
            seed=runner.run(evidence=evidence,variants=[helper.main_variant(registry)],profile=helper.profile(),
                secret='',caller=lambda **kw: (_ for _ in ()).throw(ValueError('fixture stopped')))
            identity={k:v for k,v in seed['manifest']['batch_identity'].items() if k!='execution_config'}
            batch_id='LAB-2026-09-04-'+sha256_payload(identity)[:12]
            legacy=root/'batches'/batch_id
            shutil.copytree(Path(seed['batch_root'])/'skills',legacy/'skills')
            _atomic_json(legacy/'batch_manifest.json',{**seed['manifest'],'batch_id':batch_id,
                'batch_identity':identity,'batch_identity_sha256':sha256_payload(identity)})
            docs=registry.effective_skills('main','team')
            tasks=_tasks_for_domain(evidence,'price_volume')
            prompt=_domain_prompt(domain='price_volume',tasks=tasks,documents=docs)
            schema=_report_schema();schema['properties']['results']['items']['properties']['task_id']['enum']=[t['task_id'] for t in tasks]
            runner.cache.call(profile=helper.profile(),secret='',prompt=prompt,schema=schema,caller=FakeModel())
            seen=[]
            def model(**kwargs):
                seen.append(kwargs['prompt'])
                self.assertFalse(kwargs['prompt'].startswith('INPUT FORMAT v2'))
                return FakeModel()(**kwargs)
            with patch.object(registry,'effective_skills',side_effect=AssertionError('current registry read')):
                done=runner.resume(batch_id=batch_id,evidence=evidence,profile=helper.profile(),secret='',caller=model)
            self.assertTrue(done['status']['all_variants_completed'])
            self.assertEqual(len(seen),2)
            self.assertEqual(done['manifest']['batch_identity'],identity)

    def test_failed_domain_does_not_stop_independent_domains_and_resume_uses_snapshot(self):
        from shaq_daily_oracle.research_batch import ResearchBatchRunner
        helper=fixtures.ResearchBatchTests()
        with tempfile.TemporaryDirectory() as name:
            root=Path(name); registry=helper.registry(root); evidence=helper.evidence(root)
            variant=helper.main_variant(registry)
            runner=ResearchBatchRunner(batches_root=root/'batches',cache_root=root/'cache',
                registry=registry,integration_policy=helper.policy())
            model=FakeModel(); attempted=[]
            def fail_market(**kwargs):
                if 'FROZEN TASKS:\n' in kwargs['prompt']:
                    domain=json.loads(kwargs['prompt'].split('FROZEN TASKS:\n')[1])[0]['domain']
                    attempted.append(domain)
                    if domain=='market':
                        raise ModelBackendError('timeout',diagnostic={'kind':'timeout'})
                else:self.fail('downstream call before all reports complete')
                return model(**kwargs)
            result=runner.run(evidence=evidence,variants=[variant],profile=helper.profile(),secret='',caller=fail_market)
            self.assertEqual(attempted,['market','market','price_volume'])
            self.assertFalse(result['status']['all_variants_completed'])
            self.assertEqual(len(list((Path(result['batch_root'])/'model_calls').glob('*.json'))),1)
            resumed_calls=[]
            def resume_caller(**kwargs):
                resumed_calls.append(kwargs['prompt'])
                return model(**kwargs)
            with patch.object(registry,'effective_skills',side_effect=AssertionError('mutable registry consulted')):
                resumed=runner.resume(batch_id=result['status']['batch_id'],evidence=evidence,
                    profile=helper.profile(),secret='',caller=resume_caller)
            self.assertTrue(resumed['status']['all_variants_completed'])
            self.assertEqual(len(resumed_calls),2)
            self.assertFalse(next(iter(resumed['results'].values()))['score_eligible'])
            completed=next(iter(resumed['results'].values()))['completed_at_et']
            again=runner.resume(batch_id=result['status']['batch_id'],evidence=evidence,
                profile=helper.profile(),secret='',caller=lambda **kw:self.fail('completed method recalled'))
            self.assertEqual(next(iter(again['results'].values()))['completed_at_et'],completed)
