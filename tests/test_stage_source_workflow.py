import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from test_stage_verified_assets import APP, VALIDATION, REPO, source_fixture, write_json

ROOT = Path(__file__).resolve().parents[1]


def python_step(name):
    lines = (ROOT / '.github/workflows/promote-verified-artifact.yml').read_text().splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == '- name: ' + name), None)
    if start is None:
        return None
    begin = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "python - <<'PY'")
    indent = len(lines[begin]) - len(lines[begin].lstrip())
    end = next(i for i in range(begin + 1, len(lines)) if lines[i].strip() == 'PY')
    return '\n'.join(line[indent:] for line in lines[begin + 1:end])


class StageSourceWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = source_fixture()
        self.source.pop('validation_files_sha256')
        self.env = {'GH_REPO': REPO, 'SOURCE_RUN': '101', 'SOURCE_JOB': '201',
                    'SOURCE_ARTIFACT': '301', 'SOURCE_SHA': APP, 'RELEASE_VERSION': '0.9.0',
                    'TARGET_PLATFORM': 'macOS-Intel', 'GITHUB_ENV': str(self.root / 'github-env')}
        self.api = {f'actions/runs/101': self.source['run'], 'actions/jobs/201': self.source['job'],
                    'actions/artifacts/301': self.source['artifact'],
                    'releases?per_page=100': [{'tag_name': 'lab-v0.9.0-macos', 'draft': True,
                                             'target_commitish': APP}]}

    def execute(self, name):
        script = python_step(name)
        self.assertIsNotNone(script, f'Executable workflow step missing: {name}')
        def check_output(command):
            self.assertEqual(command[:2], ['gh', 'api'])
            endpoint = command[-1]
            prefix = 'repos/' + REPO + '/'
            self.assertTrue(endpoint.startswith(prefix))
            return json.dumps(self.api[endpoint[len(prefix):]]).encode()
        old = Path.cwd()
        try:
            os.chdir(self.root)
            with mock.patch.dict(os.environ, self.env), mock.patch('subprocess.check_output', side_effect=check_output):
                exec(compile(script, '<workflow step>', 'exec'), {})
        finally:
            os.chdir(old)

    def test_api_run_job_artifact_binding_is_retained_without_rewriting_run_failure(self):
        self.execute('Verify source job, artifact and draft identity')
        path = self.root / 'native-source.json'
        self.assertTrue(path.exists(), 'Independent source metadata was not retained')
        source = json.loads(path.read_text())
        self.assertEqual(source['run']['conclusion'], 'failure')
        self.assertEqual(source['run']['head_sha'], VALIDATION)
        self.assertEqual(source['artifact']['digest'], 'sha256:' + 'c' * 64)
        self.assertEqual(source['job']['id'], 201)

    def test_api_platform_guard_rejects_an_unrelated_successful_job(self):
        self.api['actions/jobs/201']['name'] = 'native (Windows-x64)'
        with self.assertRaises(AssertionError):
            self.execute('Verify source job, artifact and draft identity')
        self.assertFalse((self.root / 'native-source.json').exists())

    def test_external_hashes_are_read_from_the_actual_run_commit_not_claimed_metadata(self):
        write_json(self.root / 'native-source.json', self.source)
        write_json(self.root / 'native-artifact/dist/diagnostic/external-layout-source.json',
                   {'application_sha': APP, 'validation_sha': 'e' * 40})
        for name, payload in [('native_layout_check.py', b'reviewed layout fixture'),
                              ('native_geometry.py', b'unchanged geometry fixture')]:
            relative = 'tests/' + name
            self.api[f'contents/{relative}?ref={VALIDATION}'] = {
                'type': 'file', 'path': relative, 'encoding': 'base64',
                'size': len(payload), 'content': base64.b64encode(payload).decode() + '\n'}
        self.execute('Read external layout hashes from the actual source run commit')
        source = json.loads((self.root / 'native-source.json').read_text())
        self.assertEqual(source['validation_files_sha256'], {
            'tests/native_layout_check.py': hashlib.sha256(b'reviewed layout fixture').hexdigest(),
            'tests/native_geometry.py': hashlib.sha256(b'unchanged geometry fixture').hexdigest()})

    def test_native_artifact_without_external_harness_needs_no_content_lookup(self):
        write_json(self.root / 'native-source.json', self.source)
        self.execute('Read external layout hashes from the actual source run commit')
        source = json.loads((self.root / 'native-source.json').read_text())
        self.assertNotIn('validation_files_sha256', source)

if __name__ == '__main__':
    unittest.main()
