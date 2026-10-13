import subprocess
import tempfile
import unittest
from pathlib import Path

import test_research_batch as fixtures
from shaq_daily_oracle.model_backends import _local_call_failure, ModelBackendError
from shaq_daily_oracle.model_execution import ExecutionPolicy
from shaq_daily_oracle.research_batch import ContentAddressedModelCache


class CliProcessRecoveryTests(unittest.TestCase):
    def test_unclassified_process_exit_has_one_retry_and_keeps_real_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            cache=ContentAddressedModelCache(Path(directory))
            calls=[]
            events=[]
            def caller(**kwargs):
                calls.append(kwargs['prompt'])
                if len(calls)==1:
                    raise _local_call_failure('Codex',subprocess.CompletedProcess([],1,'',''))
                return {'ok':True},{}
            result,audit,hit=cache.call(profile=fixtures.ResearchBatchTests().profile(),
                secret='',prompt='frozen input',schema={},caller=caller,
                on_attempt_event=lambda **event:events.append(event))
            self.assertEqual(result,{'ok':True})
            self.assertEqual(calls,['frozen input','frozen input'])
            failed=audit['attempts'][0]
            self.assertFalse(failed['transient'])
            self.assertEqual(failed['diagnostic']['kind'],'local_call')
            self.assertEqual([e['retry_reason'] for e in events if e['stage']=='model_retry_scheduled'],
                             ['local_process'])
            cache.call(profile=fixtures.ResearchBatchTests().profile(),secret='',
                prompt='frozen input',schema={},caller=lambda **kw:self.fail('completed call repeated'))

    def test_process_retry_respects_disabled_policy_and_shared_failures(self):
        failures=[
            (_local_call_failure('Codex',subprocess.CompletedProcess([],1,'','')),0),
            (ModelBackendError('login required',diagnostic={'kind':'authentication'}),1),
            (ModelBackendError('quota',diagnostic={'kind':'quota'}),1),
            (ModelBackendError('bad request',diagnostic={'kind':'http','status':400}),1),
            (ModelBackendError('missing executable',diagnostic={'kind':'executable_missing'}),1),
        ]
        for failure,retries in failures:
            with self.subTest(diagnostic=failure.diagnostic), tempfile.TemporaryDirectory() as directory:
                calls=[]
                def caller(**kwargs):
                    calls.append(1)
                    raise failure
                with self.assertRaises(ModelBackendError):
                    ContentAddressedModelCache(Path(directory)).call(
                        profile=fixtures.ResearchBatchTests().profile(),secret='',prompt='frozen',schema={},
                        caller=caller,execution_policy=ExecutionPolicy(transient_retries=retries))
                self.assertEqual(calls,[1])
