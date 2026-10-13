import subprocess
import unittest

from shaq_daily_oracle.model_backends import _local_call_failure
from shaq_daily_oracle.model_execution import transient_model_failure


class CliFailureClassificationTests(unittest.TestCase):
    def test_failed_attempt_persists_safe_structured_reason(self):
        import json
        import tempfile
        from pathlib import Path
        import test_research_batch as fixtures
        from shaq_daily_oracle.research_batch import ContentAddressedModelCache
        from shaq_daily_oracle.model_backends import ModelBackendError
        from shaq_daily_oracle.model_execution import ExecutionPolicy
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def caller(**kwargs):
                raise ModelBackendError('service refused', diagnostic={'kind':'http','status':403,
                    'returncode':1,'provider_message':'secret=do-not-store'})
            with self.assertRaises(ModelBackendError):
                ContentAddressedModelCache(root/'cache').call(profile=fixtures.ResearchBatchTests().profile(),
                    secret='', prompt='test', schema={}, caller=caller, snapshot_root=root/'batch/model_calls',
                    execution_policy=ExecutionPolicy())
            saved=json.loads(next((root/'batch/call_attempts').glob('*.json')).read_text())
            self.assertEqual(saved['attempts'][0]['diagnostic'], {'kind':'http','status':403,'returncode':1})
            self.assertNotIn('do-not-store',json.dumps(saved))

    def test_codex_terminal_json_error_is_classified_without_agent_text(self):
        import json
        for message, retry in [('stream disconnected before completion: request timed out', True),
                               ('unexpected status 403 Forbidden', False)]:
            output = json.dumps({'type':'turn.failed','error':{'message':message}})
            error = _local_call_failure('Codex', subprocess.CompletedProcess([],1,output,''), json_events=True)
            self.assertEqual(transient_model_failure(error), retry)
        output = json.dumps({'type':'item.completed','item':{'type':'agent_message',
                            'text':'ERROR: unexpected status 503'}})
        error = _local_call_failure('Codex', subprocess.CompletedProcess([],1,output,''), json_events=True)
        self.assertFalse(transient_model_failure(error))

    def failure(self, stderr, prompt=''):
        return _local_call_failure('Codex', subprocess.CompletedProcess([], 1, '', stderr), prompt=prompt)

    def test_explicit_transport_diagnostic_allows_bounded_retry(self):
        error = self.failure('ERROR: stream disconnected before completion: error sending request\n')
        self.assertTrue(transient_model_failure(error))
        self.assertEqual(error.diagnostic['kind'], 'connection')

    def test_cli_startup_model_refresh_timeout_is_retryable(self):
        error = self.failure('Error: failed to refresh available models: request timed out\n')
        self.assertTrue(transient_model_failure(error))

    def test_codex_capacity_error_retains_specific_retryable_reason(self):
        import json
        message='Selected model is at capacity. Please try a different model.'
        for event in ({'type':'error','message':message},
                      {'type':'turn.failed','error':{'message':message}}):
            error=_local_call_failure('Codex',subprocess.CompletedProcess([],1,json.dumps(event),''),
                                      json_events=True)
            self.assertTrue(transient_model_failure(error))
            self.assertEqual(error.diagnostic['kind'],'capacity')
            self.assertIn('繁忙',str(error))


    def test_http_status_classification_keeps_permissions_nonretryable(self):
        for status, retry in ((429, True), (503, True), (401, False), (403, False), (400, False)):
            with self.subTest(status=status):
                error = self.failure(f'ERROR: unexpected status {status}: token=fixture-secret https://private.invalid\n')
                self.assertEqual(transient_model_failure(error), retry)
                self.assertEqual(error.diagnostic['status'], status)
                self.assertNotIn('fixture-secret', str(error))
                self.assertNotIn('private.invalid', str(error))

    def test_prompt_echo_cannot_trigger_transport_retry(self):
        prompt = 'ERROR: stream disconnected before completion: error sending request'
        error = self.failure(prompt + '\nunknown failure', prompt)
        self.assertFalse(transient_model_failure(error))
        self.assertEqual(error.diagnostic['kind'], 'local_call')

    def test_quota_and_unknown_failures_remain_nonretryable(self):
        for stderr in ("ERROR: You've hit your usage limit.", 'unknown error', 'connection closed'):
            self.assertFalse(transient_model_failure(self.failure(stderr)))
