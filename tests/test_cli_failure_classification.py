import subprocess
import unittest

from shaq_daily_oracle.model_backends import _local_call_failure
from shaq_daily_oracle.model_execution import transient_model_failure


class CliFailureClassificationTests(unittest.TestCase):
    def failure(self, stderr, prompt=''):
        return _local_call_failure('Codex', subprocess.CompletedProcess([], 1, '', stderr), prompt=prompt)

    def test_explicit_transport_diagnostic_allows_bounded_retry(self):
        error = self.failure('ERROR: stream disconnected before completion: error sending request\n')
        self.assertTrue(transient_model_failure(error))
        self.assertEqual(error.diagnostic['kind'], 'connection')

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
