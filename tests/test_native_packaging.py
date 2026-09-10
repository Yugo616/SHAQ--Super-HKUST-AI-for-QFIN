from pathlib import Path
import importlib.util
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]


class NativePackagingTests(unittest.TestCase):
    def module(self, name):
        path = ROOT / 'packaging' / f'{name}.py'
        self.assertTrue(path.is_file(), f'missing packaging implementation: {name}')
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_common_payload_contains_complete_bundled_methods(self):
        build = self.module('build_desktop')
        args = build.pyinstaller_args(ROOT, ROOT / 'build/test', 'Test Lab')
        for resource in ('bundled_versions', 'skills', 'decision', 'config'):
            index = args.index(str(ROOT / resource) + ':' + resource)
            self.assertEqual(args[index - 1], '--add-data')
        self.assertIn(str(ROOT / 'src'), args)
        self.assertIn('zipline', args)
        # Zipline imports iso4217, which reads table.xml at import time.
        self.assertIn('iso4217', args)

    def test_audit_allows_unsupported_lzo_stub_but_rejects_actual_library(self):
        audit = self.module('audit_payload')
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / '_comp_lzo.so').write_bytes(b'stub')
            self.assertEqual(audit.forbidden_paths(root), [])
            (root / 'liblzo2.dylib').write_bytes(b'library')
            self.assertEqual(audit.forbidden_paths(root), ['liblzo2.dylib'])

    def test_audit_rejects_private_runtime_and_old_engine(self):
        audit = self.module('audit_payload')
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / 'backtrader').mkdir()
            (root / 'runtime').mkdir()
            (root / 'runtime/settings.json').write_text('{}')
            self.assertEqual(audit.forbidden_paths(root), ['backtrader', 'runtime', 'runtime/settings.json'])

    def test_macho_links_reject_external_build_paths(self):
        audit = self.module('audit_payload')
        self.assertEqual(audit.bad_macos_links(['@loader_path/libhdf5.dylib', '/usr/lib/libSystem.B.dylib', '/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation']), [])
        self.assertEqual(audit.bad_macos_links(['/opt/homebrew/lib/libhdf5.dylib']), ['/opt/homebrew/lib/libhdf5.dylib'])

    def test_vendor_runtime_allowed_but_application_runtime_rejected_in_each_layout(self):
        audit = self.module('audit_payload')
        for container in ('', '_internal', 'Contents/Resources', 'Contents/Frameworks'):
            with self.subTest(container=container), tempfile.TemporaryDirectory() as name:
                root = Path(name)
                payload = root / container
                vendor = payload / 'pythonnet/runtime/Python.Runtime.dll'
                vendor.parent.mkdir(parents=True)
                vendor.write_bytes(b'vendor runtime')
                self.assertEqual(audit.forbidden_paths(root), [])
                private = payload / 'runtime/settings.json'
                private.parent.mkdir()
                private.write_text('{}')
                self.assertEqual(audit.forbidden_paths(root), [
                    str(private.parent.relative_to(root)), str(private.relative_to(root))])

    def test_uninstall_waits_for_delayed_self_cleanup(self):
        verify = self.module('verify_uninstall')
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / 'installed'
            root.mkdir()
            timer = threading.Timer(0.05, root.rmdir)
            timer.start()
            try:
                result = verify.wait_for_uninstall(root, timeout=2, poll_interval=0.01)
            finally:
                timer.join()
            self.assertEqual(result['status'], 'passed')
            self.assertEqual(result['leftovers'], [])
            self.assertFalse(root.exists())

    def test_uninstall_fails_with_remaining_payload_or_empty_root(self):
        verify = self.module('verify_uninstall')
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / 'installed'
            root.mkdir()
            (root / 'remaining.dll').write_bytes(b'not removed')
            result = verify.wait_for_uninstall(root, timeout=0)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['leftovers'], ['.', 'remaining.dll'])
            self.assertTrue((root / 'remaining.dll').is_file())
            (root / 'remaining.dll').unlink()
            self.assertEqual(verify.wait_for_uninstall(root, timeout=0)['leftovers'], ['.'])

    def test_uninstall_cli_retains_failure_report_and_exit_status(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / 'installed'
            root.mkdir()
            report = Path(name) / 'uninstall.json'
            command = [sys.executable, str(ROOT / 'packaging/verify_uninstall.py'),
                       str(root), '--output', str(report), '--timeout-seconds', '0']
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(report.read_text())['leftovers'], ['.'])
            root.rmdir()
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual(json.loads(report.read_text())['status'], 'passed')

    def test_native_wheel_reset_preserves_sources_and_removes_all_stale_wheels(self):
        build = self.module('build_native')
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / 'sources'
            source.mkdir()
            (source / 'source.tar.gz').write_bytes(b'preserved')
            for folder in ('raw-wheels', 'wheels'):
                (root / folder).mkdir()
                (root / folder / 'old-other-architecture.whl').write_bytes(b'stale')
            paths = build.reset_wheel_directories(root)
            self.assertEqual(paths, (root / 'raw-wheels', root / 'wheels'))
            self.assertTrue(all(path.is_dir() and not list(path.iterdir()) for path in paths))
            self.assertEqual((source / 'source.tar.gz').read_bytes(), b'preserved')

    def test_gui_smoke_paths_cannot_use_user_storage(self):
        from shaq_daily_oracle import desktop
        self.assertTrue(hasattr(desktop, 'isolated_smoke_paths'))
        with tempfile.TemporaryDirectory() as name:
            paths = desktop.isolated_smoke_paths(Path(name))
            for field in ('data_root', 'config_root', 'runtime_root', 'research_root', 'skill_registry_root'):
                self.assertTrue(getattr(paths, field).is_relative_to(Path(name)))
            self.assertEqual(paths.package_root, ROOT)

    def test_native_bridge_exposes_methods_not_internal_services(self):
        from shaq_daily_oracle import desktop
        self.assertTrue(hasattr(desktop, 'desktop_api'))
        class Bridge:
            paths = Path('/private/internal')
            def get_state(self):
                return {'ok': True}
        api = desktop.desktop_api(Bridge())
        self.assertEqual(api.get_state(), {'ok': True})
        self.assertFalse(hasattr(api, 'paths'))

    def test_generated_native_metadata_maps_builder_paths_without_touching_notice(self):
        build = self.module('build_native')
        self.assertTrue(hasattr(build, 'map_build_metadata'))
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'H5build_settings.c'
            prefix = str(Path(name) / 'builder')
            path.write_text(f'Copyright HDF Group\nInstallation point: {prefix}/build\n')
            build.map_build_metadata(path, [prefix])
            self.assertEqual(path.read_text(), 'Copyright HDF Group\nInstallation point: /shaq-build/build\n')

    def test_installed_method_audit_detects_changed_and_missing_bytes(self):
        audit = self.module('audit_payload')
        self.assertTrue(hasattr(audit, 'verify_methods'))
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / 'third-party').mkdir()
            (root / 'bundled_versions').mkdir()
            method = root / 'bundled_versions/example.json'
            method.write_bytes(b'approved\n')
            (root / 'third-party/manifest.json').write_text(json.dumps({'methods': {'example.json': hashlib.sha256(b'approved\n').hexdigest()}}))
            self.assertEqual(audit.verify_methods(root), [])
            method.write_bytes(b'approved\r\n')
            self.assertEqual(audit.verify_methods(root), ['bundled method hash mismatch: example.json'])
            method.unlink()
            self.assertEqual(audit.verify_methods(root), ['bundled method missing: example.json'])
