import json
import tempfile
import unittest
import runpy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from shaq_daily_oracle import operator_control


class OperatorControlTests(unittest.TestCase):
    def test_release_scan_distinguishes_waiting_state_from_unfinished_skill(self):
        scanner = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/validate_release.py'))
        check = scanner.get('has_unfinished_prose')
        self.assertIsNotNone(check)
        self.assertFalse(check(Path('state.py'), 'state = "pending"'))
        self.assertTrue(check(Path('SKILL.md'), 'TODO: implement this method'))

    def test_manual_request_is_daily_and_never_creates_an_order(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            paths = SimpleNamespace(data_root=root, runtime_root=root / 'runtime', config_root=root)
            paths.runtime_root.mkdir()
            (root / 'operator-service.json').write_text(json.dumps({'launch_agent_label': 'org.example.operator'}))
            now = datetime(2026, 9, 9, 6, 55, tzinfo=ZoneInfo('America/New_York'))
            first = operator_control.request_today(paths, now=now, wake=lambda label: None)
            second = operator_control.request_today(paths, now=now, wake=lambda label: None)
            self.assertEqual(first['session_date'], '2026-09-09')
            self.assertTrue(second['already_requested'])
            self.assertTrue(operator_control.requested_today(paths, now.date()))
            self.assertFalse(operator_control.requested_today(paths, now.date().replace(day=10)))
            self.assertFalse(list(root.rglob('broker_journal.json')))

    def test_missing_local_operator_setup_cannot_start_broker(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            paths = SimpleNamespace(data_root=root, runtime_root=root, config_root=root)
            with self.assertRaises(ValueError):
                operator_control.request_today(paths, wake=lambda label: self.fail('must not wake'))

    def test_status_does_not_call_models_or_read_credentials_and_keeps_failure(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            paths = SimpleNamespace(data_root=root, runtime_root=root, config_root=root)
            run = root / 'SHAQ-CANARY-2026-09-09-001'
            run.mkdir()
            (run / 'workflow_state.json').write_text(json.dumps({'stages': {'release_preflight': {'status': 'failed', 'detail': 'certificate changed'}}}))
            now = datetime(2026, 9, 9, 7, 45, tzinfo=ZoneInfo('America/New_York'))
            status = operator_control.status(paths, now=now)
            self.assertEqual(status['runs'][0]['stage'], 'release_preflight')
            self.assertEqual(status['runs'][0]['detail'], 'certificate changed')
            self.assertFalse(status['runs'][0]['has_frozen_result'])
