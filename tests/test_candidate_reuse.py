import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class CandidateReuseTests(unittest.TestCase):
    def test_reuse_requires_exact_clean_source_version_and_architecture(self):
        source = Path(__file__).resolve().parents[1] / 'packaging/candidate_payload.py'
        self.assertTrue(source.is_file(), 'candidate reuse must verify provenance before packaging')
        spec = importlib.util.spec_from_file_location('candidate_payload', source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            payload = root / 'candidate'
            manifest = payload / 'third-party/manifest.json'
            manifest.parent.mkdir(parents=True)
            valid = dict(source_sha='a' * 40, source_dirty=False, version_override='1.2.3',
                         source_version='1.2.3', architecture='arm64')
            manifest.write_text(json.dumps(valid))
            with patch.object(module.subprocess, 'check_output', side_effect=lambda args, **kwargs:
                              'a' * 40 + '\n' if args[1] == 'rev-parse' else ''), \
                    patch.object(module.platform, 'machine', return_value='arm64'):
                self.assertEqual(module.verify_candidate(root, payload, '1.2.3'), payload.resolve())
                for key, value in [('source_sha', 'b' * 40), ('source_dirty', True),
                                   ('version_override', '1.2.2'), ('architecture', 'x86_64')]:
                    with self.subTest(key=key):
                        manifest.write_text(json.dumps({**valid, key: value}))
                        with self.assertRaises(ValueError):
                            module.verify_candidate(root, payload, '1.2.3')
                manifest.write_text(json.dumps(valid))
                with patch.object(module.subprocess, 'check_output', side_effect=['a' * 40 + '\n', ' M src/changed.py\n']):
                    with self.assertRaises(ValueError):
                        module.verify_candidate(root, payload, '1.2.3')
