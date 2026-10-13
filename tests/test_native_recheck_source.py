"""Only the final public-base gate may be recovered using an immutable artifact."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('native_recheck_source',
    Path(__file__).resolve().parents[1] / 'packaging/native_recheck_source.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class NativeRecheckSourceTests(unittest.TestCase):
    def setUp(self):
        self.sha = 'a' * 40
        self.platform = 'macOS-Apple-Silicon'
        self.run = {'id': 12, 'head_sha': self.sha}
        self.job = {'id': 34, 'run_id': 12, 'name': 'native (' + self.platform + ')',
                    'conclusion': 'failure', 'steps': [
                        {'name': name, 'conclusion': 'success'} for name in module.REQUIRED_STEPS
                    ] + [{'name': module.PUBLIC_STEP, 'conclusion': 'failure'}]}
        self.artifact = {'id': 56, 'name': 'SHAQ-Daily-Oracle-Lab-' + self.platform,
                         'expired': False, 'workflow_run': {'id': 12, 'head_sha': self.sha}}

    def verify(self):
        return module.validate(self.run, self.job, self.artifact, run_id=12, job_id=34,
            artifact_id=56, application_sha=self.sha, platform=self.platform)

    def test_accepts_exact_artifact_with_only_public_base_failure(self):
        self.verify()

    def test_rejects_unrelated_failed_stage(self):
        self.job['steps'][0]['conclusion'] = 'failure'
        with self.assertRaises(ValueError): self.verify()

    def test_rejects_missing_prior_acceptance(self):
        self.job['steps'].pop(0)
        with self.assertRaises(ValueError): self.verify()

    def test_rejects_different_source_artifact(self):
        self.artifact['workflow_run']['head_sha'] = 'b' * 40
        with self.assertRaises(ValueError): self.verify()

    def test_rejects_other_platform_or_job(self):
        self.job['name'] = 'native (Windows-x64)'
        with self.assertRaises(ValueError): self.verify()

    def test_rejects_skipped_public_base_gate(self):
        self.job['steps'][-1]['conclusion'] = 'skipped'
        with self.assertRaises(ValueError): self.verify()


if __name__ == '__main__': unittest.main()
