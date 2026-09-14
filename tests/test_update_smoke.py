import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from shaq_daily_oracle import update_smoke


class InstalledAcceptanceContractTests(unittest.TestCase):
    def test_process_probe_observes_real_running_process_without_signals(self):
        self.assertTrue(update_smoke.process_running(os.getpid()))

    def test_acceptance_validation_can_precede_any_data_directory_creation(self):
        with tempfile.TemporaryDirectory(prefix='shaq-installed-update-') as directory:
            root=Path(directory).resolve()
            try:
                paths=update_smoke.acceptance_paths(root,root/'installed/app',ensure=False)
            except TypeError:
                self.fail('Validate isolated paths before creating native coordination or data files')
            self.assertFalse(paths.data_root.exists())

    def driver(self):
        path = Path(__file__).parents[1] / 'packaging/installed_update_acceptance.py'
        spec = importlib.util.spec_from_file_location('acceptance_driver', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_deferred_apply_rejects_changed_program_or_incorrect_wait_state(self):
        check = getattr(update_smoke, 'verify_deferred', None)
        self.assertIsNotNone(check, 'Native acceptance must reject false waiting evidence')
        good = {'status': 'ready', 'waiting_for_idle': True,
                'message': '已下载，等待本地任务运行完更新'}
        check(good, {'program': 'same'}, {'program': 'same'}, dirty=False)
        for state, after in (({**good, 'status': 'applying'}, {'program': 'same'}),
                             (good, {'program': 'replaced'}),
                             ({**good, 'message': 'wrong'}, {'program': 'same'})):
            with self.assertRaises(RuntimeError):
                check(state, {'program': 'same'}, after, dirty=False)

    def test_coordination_event_requires_success_and_retains_payload(self):
        wait = getattr(update_smoke, 'wait_event', None)
        self.assertIsNotNone(wait, 'Native coordination must fail closed on peer errors')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'peer.json'
            with self.assertRaises(TimeoutError):
                wait(path, timeout=0)
            path.write_text(json.dumps({'status': 'failed', 'error': 'peer died'}))
            with self.assertRaisesRegex(RuntimeError, 'peer died'):
                wait(path, timeout=.1)
            path.write_text(json.dumps({'status': 'passed', 'pid': 42}))
            self.assertEqual(wait(path, timeout=.1)['pid'], 42)

    def test_rendered_target_waits_for_matching_confirmation_generation(self):
        wait = getattr(update_smoke, 'wait_restart_confirmation', None)
        self.assertIsNotNone(wait, 'Rendered DOM may precede asynchronous startup confirmation')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.json'
            old = {'version': '0.6.99', 'installation_id': 'previous', 'completed_at': 'earlier'}
            same_version_stale = {**old, 'version': '0.7.0'}
            current = {'version': '0.7.0', 'installation_id': 'current', 'completed_at': 'now'}
            path.write_text(json.dumps(old))
            delayed = iter([same_version_stale, current])
            def confirm_later(_):
                path.write_text(json.dumps(next(delayed)))
            with patch.object(update_smoke.time, 'monotonic', side_effect=[0, .1, .2]), \
                    patch.object(update_smoke.time, 'sleep', side_effect=confirm_later):
                self.assertEqual(wait(path, '0.7.0', 'current', deadline=.3), current)

    def test_restart_confirmation_keeps_existing_deadline_and_rejects_missing_stale_malformed(self):
        wait = getattr(update_smoke, 'wait_restart_confirmation', None)
        self.assertIsNotNone(wait)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.json'
            cases = [None, {'version': '0.6.99', 'installation_id': 'previous', 'completed_at': 'earlier'},
                     {'version': '0.7.0', 'installation_id': 'current', 'completed_at': ''}]
            for value in cases:
                if value is not None: path.write_text(json.dumps(value))
                with patch.object(update_smoke.time, 'monotonic', side_effect=[.9, 1.0]), \
                        patch.object(update_smoke.time, 'sleep'):
                    with self.assertRaisesRegex(TimeoutError, 'GUI-health'):
                        wait(path, '0.7.0', 'current', deadline=1.0)
            path.write_text('{malformed')
            with patch.object(update_smoke.time, 'monotonic', return_value=.9):
                with self.assertRaises(json.JSONDecodeError):
                    wait(path, '0.7.0', 'current', deadline=1.0)

    def test_missing_health_reports_retained_target_failure_without_waiting(self):
        with tempfile.TemporaryDirectory() as directory:
            events = Path(directory)
            failed = events / 'target-result.json'
            failed.write_text(json.dumps({'status': 'failed', 'error': 'confirmation rejected'}))
            with self.assertRaisesRegex(RuntimeError, 'confirmation rejected'):
                update_smoke.wait_event(events / 'target-health.json', timeout=.1, failure_path=failed)

    def test_feed_transition_never_offers_future_version(self):
        driver = self.driver()
        select = getattr(driver, 'transition_feed', None)
        self.assertIsNotNone(select, 'Consecutive native runs need separate target feeds')
        assets = [{'Version': v, 'Type': kind, 'FileName': v + kind}
                  for v in ('0.6.98', '0.6.99', '0.7.0') for kind in ('Full', 'Delta')]
        self.assertEqual(select({'Assets': assets}, '0.6.99')['Assets'], assets[2:4])
        with self.assertRaises(ValueError):
            select({'Assets': assets}, '0.8.0')

    def test_cache_acceptance_rejects_accumulated_owned_packages(self):
        check = getattr(self.driver(), 'verify_cache', None)
        self.assertIsNotNone(check, 'Repeated update must assert native cache cleanup')
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            (cache / 'current-full.nupkg').write_bytes(b'current')
            self.assertEqual(check(cache, 'current-full.nupkg'), ['current-full.nupkg'])
            (cache / 'obsolete-full.nupkg').write_bytes(b'obsolete')
            with self.assertRaises(RuntimeError):
                check(cache, 'current-full.nupkg')

    def test_program_copy_acceptance_rejects_obsolete_installation_directories(self):
        check=getattr(self.driver(),'verify_program_copies',None)
        self.assertIsNotNone(check, 'Native replacement must not accumulate old programs')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            app=root/'Example.app'
            app.mkdir()
            self.assertEqual(check(app,'darwin'), ['Example.app'])
            (root/'Example-old.app').mkdir()
            with self.assertRaises(RuntimeError):check(app,'darwin')
        with tempfile.TemporaryDirectory() as directory:
            app=Path(directory)
            (app/'current').mkdir()
            self.assertEqual(check(app,'win32'), ['current'])
            (app/'app-0.6.98').mkdir()
            with self.assertRaises(RuntimeError):check(app,'win32')
