import json
import subprocess
import tempfile
import threading
import time
import sys
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import test_research_batch as fixtures
from shaq_daily_oracle.hashing import sha256_payload
from shaq_daily_oracle.research_batch import ResearchBatchRunner, VariantSelection, _atomic_json


class DeadlineTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'win32', 'native Windows pythonw pipe acceptance')
    def test_native_pythonw_worker_pipe_roundtrip(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from shaq_daily_oracle.model_execution import run_model_process
        executable=Path(sys.executable).with_name('pythonw.exe')
        self.assertTrue(executable.is_file(),'native Windows gate requires pythonw.exe')
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                body=b'{"native":"pipe-ok"}'
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers()
                self.wfile.write(body)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            request={'operation':'http-json','payload':{'url':f'http://127.0.0.1:{server.server_port}/',
                'headers':{},'payload':{},'timeout':5}}
            result=run_model_process([str(executable),'-m','shaq_daily_oracle.model_http_worker'],
                input=json.dumps(request),text=True,encoding='utf-8',capture_output=True,timeout=10,
                env={**os.environ,'NO_PROXY':'127.0.0.1','no_proxy':'127.0.0.1'})
            self.assertEqual(result.returncode,0)
            self.assertEqual(json.loads(result.stdout),{'result':{'native':'pipe-ok'}})
        finally:
            server.shutdown();server.server_close();thread.join(timeout=1)

    def test_job_assignment_failure_never_starts_the_suspended_child(self):
        from unittest.mock import MagicMock
        from shaq_daily_oracle.model_execution import run_model_process
        process=MagicMock();process.poll.return_value=None
        owner=MagicMock();owner.assign_and_resume.side_effect=OSError('assignment denied')
        with patch('shaq_daily_oracle.model_execution.sys.platform','win32'), \
             patch('shaq_daily_oracle.model_execution.subprocess.Popen',return_value=process), \
             patch('shaq_daily_oracle.model_execution.WindowsProcessJob',return_value=owner):
            with self.assertRaisesRegex(OSError,'assignment denied'):
                run_model_process(['worker.exe'],timeout=1,capture_output=True)
        process.communicate.assert_not_called()
        process.kill.assert_called_once()
        self.assertTrue(owner.close.called)

    def test_failed_job_close_cannot_mask_the_original_model_timeout(self):
        from unittest.mock import MagicMock
        from shaq_daily_oracle.model_execution import run_model_process
        process=MagicMock();original=subprocess.TimeoutExpired('worker',1)
        process.communicate.side_effect=original
        owner=MagicMock();owner.close.side_effect=OSError('close failed')
        with patch('shaq_daily_oracle.model_execution.sys.platform','win32'), \
             patch('shaq_daily_oracle.model_execution.subprocess.Popen',return_value=process), \
             patch('shaq_daily_oracle.model_execution.WindowsProcessJob',return_value=owner):
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                run_model_process(['worker.exe'],timeout=1,capture_output=True)
        self.assertIs(caught.exception,original)

    def test_windowed_worker_reopens_pipes_without_allocating_console(self):
        import io
        from shaq_daily_oracle import model_http_worker as worker
        request=io.StringIO(json.dumps({'operation':'http-json','payload':{}}))
        response=io.StringIO()
        with patch.object(worker.sys,'stdin',None), patch.object(worker.sys,'stdout',None), \
             patch.object(worker,'_worker_stream',side_effect=[request,response],create=True), \
             patch.object(worker,'execute_transport',return_value={'ok':True}):
            self.assertEqual(worker.main(),0)
        self.assertEqual(json.loads(response.getvalue()),{'result':{'ok':True}})

    def test_frozen_worker_dispatch_happens_before_updater_and_gui_imports(self):
        import runpy
        from types import ModuleType
        worker=ModuleType('shaq_daily_oracle.model_http_worker')
        calls=[];worker.main=lambda:calls.append('worker') or 0
        with patch.dict(sys.modules,{'shaq_daily_oracle.model_http_worker':worker,'velopack':None,
                                    'shaq_daily_oracle.desktop':None}), \
             patch.object(sys,'argv',['SHAQ.exe','--model-http-worker']), patch.object(sys,'frozen',True,create=True):
            with self.assertRaises(SystemExit) as caught:
                runpy.run_path(str(Path(__file__).resolve().parents[1]/'packaging/desktop_entry.py'),run_name='__main__')
        self.assertEqual(caught.exception.code,0)
        self.assertEqual(calls,['worker'])

    def test_sdk_wrapper_uses_same_deadline_worker_and_credentials_only_stdin(self):
        from shaq_daily_oracle.model_backends import _openai_responses_call
        from shaq_daily_oracle.model_execution import execution_policy_scope, ExecutionPolicy
        from shaq_daily_oracle import model_http_worker as worker
        captured=[]
        def process(command,**kw):
            captured.append((command,kw))
            return subprocess.CompletedProcess(command,0,json.dumps({'result':[{'ok':True},{}]}),'')
        with patch.object(worker,'run_model_process',side_effect=process), \
             execution_policy_scope(ExecutionPolicy(timeout_seconds=9)):
            result,_=_openai_responses_call(profile=fixtures.ResearchBatchTests().profile(),secret='fixture-secret',prompt='packet',schema={})
        self.assertEqual(result,{'ok':True})
        command,kw=captured[0]
        self.assertEqual(kw['timeout'],9)
        self.assertNotIn('fixture-secret',str(command))
        self.assertNotIn('fixture-secret',str(kw['env']))
        payload=json.loads(kw['input'])
        self.assertEqual(payload['payload']['secret'],'fixture-secret')
        self.assertEqual(payload['operation'],'openai-responses')

    def test_windows_reader_thread_lock_is_not_closed_during_failed_cleanup(self):
        from unittest.mock import MagicMock
        from shaq_daily_oracle.model_execution import run_model_process
        process=MagicMock();process.communicate.side_effect=subprocess.TimeoutExpired('worker',1)
        process.stdout_thread.is_alive.return_value=True
        # No underscore-prefixed reader attribute exists in CPython's Windows Popen.
        del process._stdout_thread
        owner=MagicMock();owner.terminate.side_effect=OSError('job cleanup failed')
        with patch('shaq_daily_oracle.model_execution.sys.platform','win32'), \
             patch('shaq_daily_oracle.model_execution.subprocess.Popen',return_value=process), \
             patch('shaq_daily_oracle.model_execution.WindowsProcessJob',return_value=owner):
            with self.assertRaises(subprocess.TimeoutExpired):run_model_process(['worker.exe'],timeout=1,capture_output=True)
        process.stdout.close.assert_not_called()

    def test_http_continuous_body_has_actual_total_deadline(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from shaq_daily_oracle.model_backends import _http_post_json, ModelBackendError
        entered=threading.Event();closed=threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                body=b'{"ok":true,"padding":"abcdefghijklmnopqrstuvwxyz"}'
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers()
                entered.set()
                try:
                    for value in body:
                        self.wfile.write(bytes([value]));self.wfile.flush();time.sleep(.08)
                except (BrokenPipeError,ConnectionResetError):
                    closed.set()
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            started=time.monotonic()
            with patch.dict(os.environ,{'NO_PROXY':'127.0.0.1','no_proxy':'127.0.0.1'}), self.assertRaises(ModelBackendError) as caught:
                _http_post_json(url=f'http://127.0.0.1:{server.server_port}/call',headers={'Authorization':'Bearer fixture-secret'},
                    payload={'test':True},timeout=.7)
            self.assertLess(time.monotonic()-started,1.8)
            self.assertTrue(entered.is_set(),'worker did not reach the fixture HTTP server')
            self.assertEqual(caught.exception.diagnostic['kind'],'timeout')
            self.assertTrue(closed.wait(1),'HTTP child kept the socket alive after deadline')
            self.assertNotIn('fixture-secret',str(caught.exception))
        finally:
            server.shutdown();server.server_close();thread.join(timeout=1)

    def test_windows_cleanup_failure_preserves_original_timeout_without_unbounded_drain(self):
        from unittest.mock import MagicMock
        from shaq_daily_oracle.model_execution import run_model_process
        original=subprocess.TimeoutExpired(['worker.exe'],.1)
        process=MagicMock();process.pid=345;process.__enter__.return_value=process
        process.communicate.side_effect=[original, ('', '')]
        process.poll.return_value=None
        owner=MagicMock();owner.terminate.side_effect=OSError('cleanup rejected')
        with patch('shaq_daily_oracle.model_execution.sys.platform','win32'), \
             patch('shaq_daily_oracle.model_execution.subprocess.Popen',return_value=process), \
             patch('shaq_daily_oracle.model_execution.subprocess.run',side_effect=subprocess.TimeoutExpired('taskkill',10)), \
             patch('shaq_daily_oracle.model_execution.WindowsProcessJob',return_value=owner,create=True):
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                run_model_process(['worker.exe'],timeout=.1,capture_output=True)
            self.assertIs(caught.exception,original)
            process.kill.assert_called_once()
            self.assertEqual(process.communicate.call_count,1,'cleanup attempted an unbounded pipe drain')
            self.assertTrue(all(call.kwargs.get('timeout') is not None for call in process.wait.call_args_list))

    def test_early_exit_parent_cannot_leave_descendant_holding_captured_pipes(self):
        from shaq_daily_oracle.model_execution import run_model_process
        from shaq_daily_oracle.background_process import background_process_options
        with tempfile.TemporaryDirectory() as name:
            target=Path(name)/'escaped'
            child=('import sys,time; print("held-stdout",flush=True); '
                   'print("held-stderr",file=sys.stderr,flush=True); '
                   'open(sys.argv[1]+".started","w").write("child started"); '
                   'time.sleep(2); open(sys.argv[1],"w").write("orphaned")')
            parent=('import subprocess,sys; subprocess.Popen([sys.executable,"-c",sys.argv[1],sys.argv[2]],'
                    'stdout=sys.stdout,stderr=sys.stderr,'
                    'creationflags=0x08000000 if sys.platform=="win32" else 0)')
            # Prove this exact fixture leaves captured pipes open after its
            # parent exits. Windows close_fds does not inherit those handles
            # without the explicit standard-stream redirection above.
            control=subprocess.Popen([sys.executable,'-c',parent,child,str(Path(name)/'control')],
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,**background_process_options())
            try:
                self.assertEqual(control.wait(timeout=1),0,'fixture parent did not exit early')
                with self.assertRaises(subprocess.TimeoutExpired):
                    control.communicate(timeout=.1)
            finally:
                output,errors=control.communicate(timeout=4)
            self.assertEqual(output.strip(),b'held-stdout')
            self.assertEqual(errors.strip(),b'held-stderr')
            unrelated=subprocess.Popen([sys.executable,'-c','import time; time.sleep(10)'],**background_process_options())
            try:
                started=time.monotonic()
                with self.assertRaises(subprocess.TimeoutExpired):
                    run_model_process([sys.executable,'-c',parent,child,str(target)],timeout=.4,capture_output=True)
                self.assertLess(time.monotonic()-started,1.5)
                self.assertTrue(Path(str(target)+'.started').is_file(), 'owned descendant did not start')
                self.assertIsNone(unrelated.poll())
                time.sleep(2)
                self.assertFalse(target.exists(),'orphaned descendant survived the original process deadline')
            finally:
                unrelated.terminate();unrelated.wait(timeout=2)


class DownstreamCheckpointTests(unittest.TestCase):
    def test_shared_cache_reconciliation_rejects_valid_conflicts_inputs_and_tampering(self):
        import copy
        from shaq_daily_oracle.research_batch import ContentAddressedModelCache, ResearchBatchError, _validated_adversary
        helper=fixtures.ResearchBatchTests()
        for mutation in ['valid-conflict','changed-input','tampered']:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as name:
                root=Path(name);cache=ContentAddressedModelCache(root/'cache');profile=helper.profile()
                prompt='REPORTS:\n{"AAPL":[]}'
                _,audit,_=cache.call(profile=profile,secret='',prompt=prompt,schema={},caller=fixtures.FakeModel())
                global_path=root/'cache'/audit['request_policy_sha256']/f"{audit['cache_key']}.json"
                global_bytes=global_path.read_bytes();local=copy.deepcopy(json.loads(global_bytes))
                local['result']['results'][0]['report']['strongest_countercase']='另一个同样合法但不相同的反方判断'
                if mutation!='tampered':
                    local['result_sha256']=sha256_payload(local['result'])
                    local['audit']['output_sha256']=local['result_sha256']
                    local['audit_sha256']=sha256_payload(local['audit'])
                    if mutation=='changed-input':local['prompt']='different frozen input'
                    local['cache_document_sha256']=sha256_payload({k:v for k,v in local.items() if k!='cache_document_sha256'})
                snapshots=root/'batch/model_calls';local_path=snapshots/global_path.name
                _atomic_json(local_path,local);local_bytes=local_path.read_bytes()
                with self.assertRaises(ResearchBatchError):
                    cache.call(profile=profile,secret='',prompt=prompt,schema={},
                        caller=lambda **kw:self.fail('conflicting frozen data was recalled'),snapshot_root=snapshots,
                        recover_rejected_cache=True,validate=lambda value:_validated_adversary(value,{'AAPL':[]}))
                self.assertEqual(global_path.read_bytes(),global_bytes)
                self.assertEqual(local_path.read_bytes(),local_bytes)
                self.assertFalse((root/'batch/rejected_model_calls').exists())

    def test_two_old_batches_share_repaired_global_checkpoint_without_recalling_valid_model(self):
        helper=fixtures.ResearchBatchTests()
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);registry=helper.registry(root);evidence=helper.evidence(root)
            runner=ResearchBatchRunner(batches_root=root/'batches',cache_root=root/'cache',
                registry=registry,integration_policy=helper.policy())
            original_call=runner.cache.call
            def legacy_call(**kw):
                kw.pop('validate',None)
                return original_call(**kw)
            def invalid(**kw):
                return fixtures.FakeModel()(**kw) if 'FROZEN TASKS:' in kw['prompt'] else ({'results':[]},{})
            old=[]
            for version in ['old-app-A','old-app-B']:
                with patch.object(runner.cache,'call',side_effect=legacy_call), \
                     patch('shaq_daily_oracle.research_batch._application_version',return_value=version):
                    old.append(runner.run(evidence=evidence,variants=[helper.main_variant(registry)],
                        profile=helper.profile(),secret='',caller=invalid))
            self.assertNotEqual(old[0]['batch_root'],old[1]['batch_root'])
            batch_b=Path(old[1]['batch_root'])
            before={path.name:path.read_bytes() for path in (batch_b/'model_calls').glob('*.json')}
            rejected=next(json.loads(content) for content in before.values() if 'REPORTS:' in json.loads(content)['prompt'])
            first=runner.resume(batch_id=old[0]['status']['batch_id'],evidence=evidence,profile=helper.profile(),secret='',caller=fixtures.FakeModel())
            self.assertTrue(first['status']['all_variants_completed'])
            second=runner.resume(batch_id=old[1]['status']['batch_id'],evidence=evidence,profile=helper.profile(),
                secret='',caller=lambda **kw:self.fail('repaired valid global checkpoint was recalled'))
            self.assertTrue(second['status']['all_variants_completed'])
            self.assertTrue(any(json.loads(path.read_text())==rejected for path in (batch_b/'rejected_model_calls').glob('*.json')))
            for filename,content in before.items():
                if json.loads(content)['cache_key'] != rejected['cache_key']:
                    self.assertEqual((batch_b/'model_calls'/filename).read_bytes(),content)
            repaired=json.loads((batch_b/'model_calls'/f"{rejected['cache_key']}.json").read_text())
            self.assertEqual(repaired,json.loads((Path(first['batch_root'])/'model_calls'/f"{rejected['cache_key']}.json").read_text()))

    def test_invalid_adversary_is_not_promoted_and_resume_retries_only_adversary(self):
        helper=fixtures.ResearchBatchTests()
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);registry=helper.registry(root);evidence=helper.evidence(root)
            runner=ResearchBatchRunner(batches_root=root/'batches',cache_root=root/'cache',
                registry=registry,integration_policy=helper.policy())
            def invalid(**kw):
                return fixtures.FakeModel()(**kw) if 'FROZEN TASKS:' in kw['prompt'] else ({'results':[]},{})
            first=runner.run(evidence=evidence,variants=[helper.main_variant(registry)],profile=helper.profile(),secret='',caller=invalid)
            self.assertFalse(first['status']['all_variants_completed'])
            stored=list((Path(first['batch_root'])/'model_calls').glob('*.json'))
            self.assertEqual(len(stored),2,'rejected adversary was promoted as a completed call')
            before={p.name:p.read_bytes() for p in stored};seen=[]
            def valid(**kw):
                seen.append(kw['prompt']);return fixtures.FakeModel()(**kw)
            done=runner.resume(batch_id=first['status']['batch_id'],evidence=evidence,profile=helper.profile(),secret='',caller=valid)
            self.assertTrue(done['status']['all_variants_completed'])
            self.assertEqual(len(seen),1)
            self.assertIn('REPORTS:',seen[0])
            self.assertTrue(all((Path(first['batch_root'])/'model_calls'/p).read_bytes()==value for p,value in before.items()))

    def test_invalid_synthesis_is_not_promoted(self):
        from shaq_daily_oracle.bundled_versions import install_bundled_versions
        helper=fixtures.ResearchBatchTests()
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);registry=helper.registry(root);install_bundled_versions(registry);evidence=helper.evidence(root)
            variant=next(VariantSelection.from_registry_row(r) for r in registry.list_method_versions()
                         if r['version_id']=='cross-domain-synthesis-1')
            runner=ResearchBatchRunner(batches_root=root/'batches',cache_root=root/'cache',registry=registry,integration_policy=helper.policy())
            def invalid(**kw):
                return ({'decisions':[]},{}) if 'FROZEN SYNTHESIS INPUT:' in kw['prompt'] else fixtures.FakeModel()(**kw)
            first=runner.run(evidence=evidence,variants=[variant],profile=helper.profile(),secret='',caller=invalid)
            self.assertFalse(first['status']['all_variants_completed'])
            stored=list((Path(first['batch_root'])/'model_calls').glob('*.json'))
            self.assertEqual(len(stored),3,'rejected synthesis was promoted as completed')
            seen=[]
            def valid(**kw):
                seen.append(kw['prompt'])
                self.assertIn('FROZEN SYNTHESIS INPUT:',kw['prompt'])
                return {'decisions':[{'symbol':'AAPL','action':'reject','direction':'neutral','thesis':'不足',
                    'antithesis':'可能反向','resolution':'不发布','comparison':'唯一候选','unknowns':[],
                    'invalidation':[],'evidence_ids':[]}]},{}
            done=runner.resume(batch_id=first['status']['batch_id'],evidence=evidence,profile=helper.profile(),secret='',caller=valid)
            self.assertTrue(done['status']['all_variants_completed'])
            self.assertEqual(len(seen),1)

    def test_explicit_resume_quarantines_legacy_invalid_checkpoint_and_preserves_diagnostics(self):
        helper=fixtures.ResearchBatchTests()
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);registry=helper.registry(root);evidence=helper.evidence(root)
            runner=ResearchBatchRunner(batches_root=root/'batches',cache_root=root/'cache',registry=registry,integration_policy=helper.policy())
            # Model a pre-fix batch: downstream semantic validator was absent during cache promotion.
            original_call=runner.cache.call
            def legacy_call(**kw):
                kw.pop('validate',None)
                return original_call(**kw)
            def invalid(**kw):
                return fixtures.FakeModel()(**kw) if 'FROZEN TASKS:' in kw['prompt'] else ({'results':[]},{})
            with patch.object(runner.cache,'call',side_effect=legacy_call):
                first=runner.run(evidence=evidence,variants=[helper.main_variant(registry)],profile=helper.profile(),secret='',caller=invalid)
            batch=Path(first['batch_root'])
            rejected=next(json.loads(p.read_text()) for p in (batch/'model_calls').glob('*.json')
                          if 'REPORTS:' in json.loads(p.read_text())['prompt'])
            self.assertFalse(first['status']['all_variants_completed'])
            seen=[]
            def valid(**kw):
                seen.append(kw['prompt']);return fixtures.FakeModel()(**kw)
            done=runner.resume(batch_id=first['status']['batch_id'],evidence=evidence,profile=helper.profile(),secret='',caller=valid)
            self.assertTrue(done['status']['all_variants_completed'])
            self.assertEqual(len(seen),1)
            preserved=list((batch/'rejected_model_calls').glob('*.json'))
            self.assertTrue(any(json.loads(p.read_text())==rejected for p in preserved))
            canonical=json.loads((batch/'model_calls'/f"{rejected['cache_key']}.json").read_text())
            self.assertNotEqual(canonical['result'],{'results':[]})
