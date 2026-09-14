import importlib.util
from pathlib import Path
import unittest
import ctypes
import json
import tempfile
from unittest.mock import patch


class WindowsCIDisplayTests(unittest.TestCase):
    def module(self):
        path = Path(__file__).resolve().parents[1]/'packaging/windows_ci_display.py'
        self.assertTrue(path.exists(), 'CI display provisioning is missing')
        spec = importlib.util.spec_from_file_location('ci_display', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_fixed_1024_host_fails_with_before_modes_and_after_evidence(self):
        module = self.module()
        mode = {'width':1024, 'height':768, 'bits':32, 'frequency':60}
        class Host:
            def current(self): return mode
            def modes(self): return [mode]
            def change(self, mode, test): raise AssertionError('unsupported mode must not be invented')
        report = {}
        with self.assertRaisesRegex(RuntimeError, 'No supported display mode'):
            module.provision(Host(), 1520, 900, report)
        self.assertEqual(report['before'], mode)
        self.assertEqual(report['after'], mode)
        self.assertEqual(report['supported_modes'], [mode])

    def test_win32_devmode_layout_and_non_ci_guard(self):
        module = self.module()
        self.assertEqual(ctypes.sizeof(module.DevMode), 220)
        self.assertEqual(module.DevMode.size.offset, 68)
        self.assertEqual(module.DevMode.width.offset, 172)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'display.json'
            with patch.object(module.sys, 'platform', 'win32'), patch.dict(module.os.environ, {}, clear=True), \
                    patch.object(module.sys, 'argv', ['display', '--minimum-width','1520','--minimum-height','900','--output',str(output)]), \
                    patch.object(module, 'WindowsDisplay', side_effect=AssertionError('must not access user display')):
                self.assertEqual(module.main(), 1)
            self.assertIn('restricted', json.loads(output.read_text())['error'])

    def test_uses_enumerated_mode_tests_then_applies_and_checks_actual_result(self):
        module = self.module()
        small = {'width':1024, 'height':768, 'bits':32, 'frequency':60}
        large = dict(small, width=1600, height=900)
        for clamp in (False, True):
            class Host:
                state = small
                calls = []
                def current(self): return self.state
                def modes(self): return [small, large]
                def change(self, mode, test):
                    self.calls.append((mode, test))
                    if not test and not clamp: self.state = mode
                    return 0
            host = Host(); report = {}
            if clamp:
                with self.assertRaisesRegex(RuntimeError, 'still too small'):
                    module.provision(host, 1520, 900, report)
            else:
                module.provision(host, 1520, 900, report)
            self.assertEqual(host.calls, [(large, True), (large, False)])
            self.assertEqual(report['after'], small if clamp else large)
