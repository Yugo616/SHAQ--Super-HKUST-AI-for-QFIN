"""Progress events describe actual model attempts, not a new retry policy."""
from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from shaq_daily_oracle.model_backends import ModelBackendError
from shaq_daily_oracle.model_execution import ExecutionPolicy
from shaq_daily_oracle.research_batch import ResearchBatchRunner
from shaq_daily_oracle.research_progress import ResearchProgressLog
import test_research_batch as fixtures


class ModelRetryProgressTests(unittest.TestCase):
    def run_batch(self, caller):
        helper = fixtures.ResearchBatchTests()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        registry = helper.registry(root)
        runner = ResearchBatchRunner(
            batches_root=root / 'batches', cache_root=root / 'cache',
            registry=registry, integration_policy=helper.policy())
        log = ResearchProgressLog(root / 'research_progress.jsonl')
        result = runner.run(evidence=helper.evidence(root),
                            variants=[helper.main_variant(registry)],
                            profile=helper.profile(), secret='secret', caller=caller,
                            execution_policy=ExecutionPolicy(transient_retries=1),
                            observer=log.append)
        return result, log.read()

    def test_transient_domain_failure_emits_failed_scheduled_and_started_attempts(self):
        normal = fixtures.FakeModel()
        calls = 0
        def caller(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ModelBackendError('timeout', diagnostic={'kind': 'timeout'})
            return normal(**kwargs)

        result, events = self.run_batch(caller)
        self.assertIn('team/main', result['results'])
        failed = [event for event in events if event['stage'] == 'model_attempt_failed']
        scheduled = [event for event in events if event['stage'] == 'model_retry_scheduled']
        restarted = [event for event in events if event['stage'] == 'model_retry_started']
        self.assertEqual(len(failed), 1)
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(len(restarted), 1)
        for event, attempt in [(failed[0], 1), (scheduled[0], 2), (restarted[0], 2)]:
            self.assertEqual(event['attempt'], attempt)
            self.assertEqual(event['max_attempts'], 2)
            self.assertEqual(event['domain'], failed[0]['domain'])
            self.assertEqual(event['symbols'], failed[0]['symbols'])
            self.assertEqual(event['call_id'], failed[0]['call_id'])
        self.assertEqual(restarted[0]['status'], 'running')
        self.assertNotIn('next_retry_at', scheduled[0],
                         'the next rate slot is not reserved at scheduling time')
        self.assertEqual(scheduled[0]['waiting_for'], 'rate_slot')
        returned = [event for event in events if event['stage'] == 'model_returned' and
                    event['call_id'] == failed[0]['call_id']]
        self.assertEqual(len(returned), 1)
        self.assertEqual(returned[0]['attempt'], 2)

    def test_nontransient_auth_failure_has_no_scheduled_retry(self):
        def caller(**kwargs):
            raise ModelBackendError('forbidden', diagnostic={'kind': 'permission', 'status': 403})

        _, events = self.run_batch(caller)
        self.assertTrue(any(event['stage'] == 'model_attempt_failed' for event in events))
        self.assertFalse(any(event['stage'] in {'model_retry_scheduled', 'model_retry_started'}
                             for event in events))

    def test_adversary_retry_uses_same_saved_attempt_events(self):
        normal = fixtures.FakeModel()
        failed_once = False
        def caller(**kwargs):
            nonlocal failed_once
            if 'REPORTS:\n' in kwargs['prompt'] and not failed_once:
                failed_once = True
                raise ModelBackendError('timeout', diagnostic={'kind': 'timeout'})
            return normal(**kwargs)

        result, events = self.run_batch(caller)
        self.assertIn('team/main', result['results'])
        adversary = [event for event in events if event.get('domain') == 'adversary']
        self.assertEqual([event['stage'] for event in adversary if event['stage'] in {
            'model_attempt_failed', 'model_retry_scheduled', 'model_retry_started'}],
            ['model_attempt_failed', 'model_retry_scheduled', 'model_retry_started'])
        self.assertTrue(all(event['call_id'] for event in adversary))


if __name__ == '__main__':
    unittest.main()
