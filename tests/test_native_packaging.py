from pathlib import Path
import importlib.util
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
import base64
import io
import inspect
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class NativePackagingTests(unittest.TestCase):
    def test_upstream_proof_requires_archive_record_and_installed_bytes_to_agree(self):
        build = self.module('build_desktop')
        home = '/'.join(('C:', 'Users', 'runneradmin'))
        data = (home + '/AppData/Local/Temp/build-env-abc/lib/numpy/source.pxd').encode()
        digest = hashlib.sha256(data).digest()
        with tempfile.TemporaryDirectory() as name:
            wheel = Path(name) / 'upstream.whl'
            with zipfile.ZipFile(wheel, 'w') as archive:
                archive.writestr('vendor/core.pyd', data)
                archive.writestr('vendor-1.dist-info/RECORD', 'vendor/core.pyd,sha256=' +
                    base64.urlsafe_b64encode(digest).decode().rstrip('=') + ',' + str(len(data)) + '\n')
            sha = hashlib.sha256(wheel.read_bytes()).hexdigest()
            self.assertEqual(build.verified_wheel_files(wheel, sha, {'vendor/core.pyd': data}),
                             {'vendor/core.pyd': digest.hex()})
            self.assertEqual(build.verified_wheel_files(wheel, sha, {'vendor/core.pyd': data + b'changed'}), {})
            with self.assertRaisesRegex(ValueError, 'archive'):
                build.verified_wheel_files(wheel, '0' * 64, {'vendor/core.pyd': data})
            with zipfile.ZipFile(wheel, 'w') as archive:
                archive.writestr('vendor/core.pyd', data)
                archive.writestr('vendor-1.dist-info/RECORD', 'vendor/core.pyd,sha256=invalid,' + str(len(data)) + '\n')
            with self.assertRaisesRegex(ValueError, 'RECORD'):
                build.verified_wheel_files(wheel, hashlib.sha256(wheel.read_bytes()).hexdigest(), {'vendor/core.pyd': data})

    def test_collected_provenance_excludes_local_builds_and_keeps_public_wheel_proof(self):
        build = self.module('build_desktop')
        from importlib.metadata import PathDistribution
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            package = root / 'vendor'
            package.mkdir()
            home = '/'.join(('C:', 'Users', 'runneradmin'))
            data = (home + '/AppData/Local/Temp/build-env-abc/numpy/source.pxd').encode()
            (package / 'core.pyd').write_bytes(data)
            dist_info = root / 'vendor-1.0.dist-info'
            dist_info.mkdir()
            (dist_info / 'METADATA').write_text('Name: vendor\nVersion: 1.0\n', encoding='utf-8')
            (dist_info / 'WHEEL').write_text('Tag: cp313-cp313-win_amd64\n', encoding='utf-8')
            digest = hashlib.sha256(data).digest()
            record = 'vendor/core.pyd,sha256=' + base64.urlsafe_b64encode(digest).decode().rstrip('=') + ',' + str(len(data)) + '\n'
            (dist_info / 'RECORD').write_text(record, encoding='utf-8')
            wheel = io.BytesIO()
            with zipfile.ZipFile(wheel, 'w') as archive:
                archive.writestr('vendor/core.pyd', data)
                archive.writestr('vendor-1.0.dist-info/RECORD', record)
            wheel_bytes = wheel.getvalue()
            release = {'filename': 'vendor-1.0-cp313-cp313-win_amd64.whl', 'packagetype': 'bdist_wheel',
                       'url': 'https://files.pythonhosted.org/vendor.whl',
                       'digests': {'sha256': hashlib.sha256(wheel_bytes).hexdigest()}}
            def response(url, **kwargs):
                return io.BytesIO(wheel_bytes if url == release['url'] else json.dumps({'urls': [release]}).encode())
            destination = root / 'notices'
            destination.mkdir()
            with patch.object(build.sys, 'platform', 'win32'), patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'RUNNER_ENVIRONMENT': 'github-hosted'}), patch.object(build.Path, 'home', return_value=Path(home)), patch.object(build.metadata, 'distributions', return_value=[PathDistribution(dist_info)]), patch.object(build.urllib.request, 'urlopen', side_effect=response):
                build.collect_upstream_path_provenance(destination)
                proofs = json.loads((destination / 'upstream-path-provenance.json').read_text(encoding='utf-8'))
                self.assertEqual(proofs[0]['files'], {'vendor/core.pyd': digest.hex()})
                self.assertEqual(proofs[0]['wheel_sha256'], release['digests']['sha256'])
                self.assertEqual(proofs[0]['wheel_url'], release['url'])
                (dist_info / 'direct_url.json').write_text('{"url":"file:///local.whl"}', encoding='utf-8')
                build.collect_upstream_path_provenance(destination)
                self.assertEqual(json.loads((destination / 'upstream-path-provenance.json').read_text(encoding='utf-8')), [])

    def test_windows_common_notice_recipe_emits_upstream_provenance(self):
        build = self.module('build_desktop')
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            for file in ('packaging/native-sources.json', 'build/native-dependencies/mingw-toolchain.json',
                         'build/native-dependencies/hdf5-diagnostic-map.json', 'docs/third-party-notices.md'):
                target = root / file
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('{}', encoding='utf-8')
            (root / 'build/third-party/hdf5').mkdir(parents=True)
            def collect_mingw(destination, provenance):
                (destination / 'hdf5').mkdir()
            with patch.object(build.sys, 'platform', 'win32'), patch.object(build.metadata, 'distributions', return_value=[]), patch.object(build, 'collect_mingw_notices', side_effect=collect_mingw), patch.object(build.subprocess, 'check_output', side_effect=['source-sha', '']):
                build.collect_notices(root)
            self.assertEqual(json.loads((root / 'build/third-party/upstream-path-provenance.json').read_text(encoding='utf-8')), [])

    def test_verified_wheel_class_does_not_exempt_checkout_or_personal_paths(self):
        audit = self.module('audit_payload')
        home, checkout = '/'.join(('C:', 'Users', 'runneradmin')), 'D:/a/our-project/our-project'
        data = (home + '/AppData/Local/Temp/build-env-abc/lib/site-packages/numpy/source.pxd').encode()
        for value in (data, data.replace(b'/', b'\\').upper()):
            self.assertTrue(audit.contains_private_path(value, home, checkout, True))
            self.assertFalse(audit.contains_private_path(value, home, checkout, True, verified_upstream=True))
            self.assertTrue(audit.contains_private_path(value + checkout.encode(), home, checkout, True, verified_upstream=True))
        self.assertTrue(audit.contains_private_path(data, home, checkout, False, verified_upstream=True))
        for suffix in ('/.ssh/id_rsa', '/AppData/Local/Temp/my-private-file'):
            personal = '/'.join(('C:', 'Users', 'personal'))
            self.assertTrue(audit.contains_private_path((personal + suffix).encode(), personal, checkout, True, verified_upstream=True))
        self.assertTrue(audit.contains_private_path((home + '/.ssh/id_rsa').encode(), home, checkout, True, verified_upstream=True))

    def test_payload_proof_requires_matching_filename_and_unchanged_digest(self):
        audit = self.module('audit_payload')
        home = '/'.join(('C:', 'Users', 'runneradmin'))
        data = (home + '/AppData/Local/Temp/build-env-abc/numpy/source.pxd').encode()
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            target = root / 'vendor/core.pyd'
            target.parent.mkdir()
            target.write_bytes(data)
            manifest = root / 'third-party/upstream-path-provenance.json'
            manifest.parent.mkdir()
            proof = [{'files': {'vendor/core.pyd': hashlib.sha256(data).hexdigest()}}]
            with patch.object(audit.Path, 'home', return_value=Path(home)), patch.object(audit.sys, 'platform', 'test'), patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'RUNNER_ENVIRONMENT': 'github-hosted'}):
                def private_failures():
                    return [failure for failure in audit.audit(root)['failures'] if failure.startswith('private build/user path:')]
                self.assertEqual(private_failures(), ['private build/user path: vendor/core.pyd'])
                manifest.write_text(json.dumps(proof), encoding='utf-8')
                self.assertEqual(private_failures(), [])
                target.write_bytes(data + b'changed')
                self.assertEqual(private_failures(), ['private build/user path: vendor/core.pyd'])
                target.write_bytes(data)
                target.rename(root / 'vendor/renamed.pyd')
                self.assertEqual(private_failures(), ['private build/user path: vendor/renamed.pyd'])

    def mingw_fixture(self, root):
        prefix = root / 'action-location/mingw64'
        compiler = prefix / 'bin/gcc.exe'
        compiler.parent.mkdir(parents=True)
        compiler.write_bytes(b'configured MSYS2 compiler')
        notices = {'gcc-libs': ('COPYING3', 'COPYING.LIB', 'COPYING.RUNTIME'),
                   'crt': ('COPYING', 'COPYING.MinGW-w64.txt', 'COPYING.MinGW-w64-runtime.txt'),
                   'headers': ('COPYING', 'COPYING.MinGW-w64.txt', 'COPYING.MinGW-w64-runtime.txt'),
                   'winpthreads': ('COPYING',), 'libwinpthread': ('COPYING',)}
        for component, names in notices.items():
            for name in names:
                target = prefix / 'share/licenses' / component / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(f'upstream {component} {name}'.encode())
        for component in ('gcc', *notices):
            package = 'mingw-w64-x86_64-' + component
            target = prefix.parent / 'var/lib/pacman/local' / (package + '-1.0-1/desc')
            target.parent.mkdir(parents=True)
            target.write_text(f'%NAME%\n{package}\n\n%VERSION%\n1.0-1\n\n%BASE%\nmingw-w64-test\n', encoding='utf-8')
        return prefix

    def test_mingw_notices_and_compiler_use_same_explicit_msys2_prefix(self):
        build = self.module('build_desktop')
        self.assertTrue(hasattr(build, 'mingw_toolchain'))
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            prefix = self.mingw_fixture(root)
            with patch.dict(os.environ, {'SHAQ_MINGW_PREFIX': str(prefix), 'PATH': str(root / 'other/bin')}):
                actual, env, provenance = build.mingw_toolchain()
                self.assertEqual(actual, prefix.resolve())
                self.assertEqual(env['PATH'].split(os.pathsep)[0], str(prefix.resolve() / 'bin'))
                build.collect_mingw_notices(root / 'notices', provenance)
            self.assertEqual((root / 'notices/MinGW-W64/gcc-libs/COPYING.RUNTIME').read_bytes(),
                             b'upstream gcc-libs COPYING.RUNTIME')
            manifest = json.loads((root / 'notices/MinGW-W64/toolchain.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['packages']['mingw-w64-x86_64-gcc-libs']['version'], '1.0-1')
            self.assertEqual(manifest['compiler_sha256'], hashlib.sha256(b'configured MSYS2 compiler').hexdigest())
            self.assertNotIn(str(root), json.dumps(manifest))

    def test_mingw_missing_notices_or_changed_compiler_fail_closed(self):
        build = self.module('build_desktop')
        self.assertTrue(hasattr(build, 'mingw_toolchain'))
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            prefix = self.mingw_fixture(root)
            with patch.dict(os.environ, {'SHAQ_MINGW_PREFIX': str(prefix)}):
                _, _, provenance = build.mingw_toolchain()
                (prefix / 'bin/gcc.exe').write_bytes(b'changed compiler')
                with self.assertRaisesRegex(RuntimeError, 'changed'):
                    build.collect_mingw_notices(root / 'notices', provenance)
                notice = prefix / 'share/licenses/gcc-libs/COPYING.RUNTIME'
                notice.unlink()
                with self.assertRaisesRegex(RuntimeError, 'COPYING.RUNTIME'):
                    build.mingw_toolchain()
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(RuntimeError, 'SHAQ_MINGW_PREFIX'):
                    build.mingw_toolchain()

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

    def test_macos_loader_binary_uses_repaired_bytes_at_upstream_search_name(self):
        build = self.module('build_desktop')
        self.assertTrue(hasattr(build, 'stage_macos_blosc2'))
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            tables = root / 'tables'
            (tables / '.dylibs').mkdir(parents=True)
            library = tables / '.dylibs/libblosc2.7.dylib'
            library.write_bytes(b'repaired native bytes')
            staged = build.stage_macos_blosc2(tables, root / 'stage')
            self.assertEqual(staged.name, 'libblosc2.dylib')
            self.assertEqual(staged.read_bytes(), library.read_bytes())
            args = build.pyinstaller_args(ROOT, root / 'dist', 'Test', blosc2_library=staged)
            index = args.index(f'{staged}:tables')
            self.assertEqual(args[index - 1], '--add-binary')
            library.unlink()
            with self.assertRaises(RuntimeError):
                build.stage_macos_blosc2(tables, root / 'stage')

    def test_bcolz_build_uses_separate_pinned_environment(self):
        from packaging.requirements import Requirement
        build = self.module('build_native')
        lock = ROOT / 'packaging/bcolz-build.lock.txt'
        self.assertTrue(lock.is_file())
        versions = {}
        for line in lock.read_text().splitlines():
            if line and not line.startswith('#'):
                req = Requirement(line)
                versions[req.name.lower().replace('_', '-')] = next(iter(req.specifier)).version
        # Build-system requirements from the pinned upstream bcolz 1.13.0 sdist.
        for requirement in ('setuptools>=45', 'setuptools_scm[toml]>=6.2', 'wheel',
                            'Cython>=0.22,<3.2.0', 'toml', 'numpy>=2.0.0rc1'):
            req = Requirement(requirement)
            self.assertIn(versions[req.name.lower().replace('_', '-')], req.specifier)
        with tempfile.TemporaryDirectory() as name, patch.object(build, 'run') as run:
            output = Path(name)
            build.build_bcolz(ROOT, output / 'source', output / 'wheels', output / 'env')
            commands = [tuple(str(arg) for arg in call.args) for call in run.call_args_list]
            self.assertEqual(commands[0], (sys.executable, '-m', 'venv', str(output / 'env')))
            self.assertTrue(all(command[0] != sys.executable for command in commands[1:]))
            self.assertIn(str(lock), commands[1])
            self.assertIn('--no-build-isolation', commands[2])
            self.assertIn(str(output / 'source'), commands[2])

    def test_windows_repaired_wheel_retains_blosc2_loader_name(self):
        build = self.module('build_native')
        self.assertTrue(hasattr(build, 'verify_windows_blosc2'))
        with tempfile.TemporaryDirectory() as name:
            wheel = Path(name) / 'tables.whl'
            for entry in ('tables/libblosc2.dll', 'tables.libs/libblosc2.dll'):
                with zipfile.ZipFile(wheel, 'w') as archive:
                    archive.writestr(entry, b'vendored DLL')
                build.verify_windows_blosc2(wheel)
            with zipfile.ZipFile(wheel, 'w') as archive:
                archive.writestr('tables.libs/libblosc2-hashed.dll', b'vendored DLL')
            with self.assertRaises(RuntimeError):
                build.verify_windows_blosc2(wheel)

    def test_frozen_blosc2_guard_rejects_external_fallback(self):
        hook = ROOT / 'packaging/frozen_native.py'
        self.assertTrue(hook.is_file())
        with tempfile.TemporaryDirectory() as name:
            code = ('import runpy,sys; sys.frozen=True; sys._MEIPASS=sys.argv[2]; '
                    'runpy.run_path(sys.argv[1]); sys.audit("ctypes.dlopen",sys.argv[3])')
            root = Path(name)
            for library, expected in ((root / 'tables/libblosc2.dylib', 0),
                                      (root / 'tables.libs/libblosc2.dll', 0),
                                      (root.parent / 'kernel32.dll', 0),
                                      (root.parent / 'libblosc2.dylib', 1),
                                      (Path('libblosc2.dll'), 1)):
                result = subprocess.run([sys.executable, '-c', code, str(hook), name, str(library)],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stderr)

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

    def test_hosted_runner_vendor_paths_do_not_hide_current_checkout_or_private_data(self):
        audit = self.module('audit_payload')
        self.assertTrue(hasattr(audit, 'contains_private_path'))
        home = '/'.join(('', 'Users', 'runner'))
        checkout = home + '/work/current/current'
        vendor = (home + '/work/h5py/h5py/lzf/lzf_filter.c\0' +
                  home + '/.cargo/registry/src/index.crates.io/pyo3/src/err.rs').encode()
        self.assertFalse(audit.contains_private_path(vendor, home, checkout, hosted_runner=True))
        self.assertTrue(audit.contains_private_path(vendor, home, checkout, hosted_runner=False))
        for private in (checkout + '/src/app.py', home + '/Library/settings.json', home + '/.ssh/id_ed25519',
                        home + '/work/_temp/credentials'):
            self.assertTrue(audit.contains_private_path(vendor + b'\0' + private.encode(), home, checkout, hosted_runner=True))
        with tempfile.TemporaryDirectory() as personal:
            self.assertTrue(audit.contains_private_path((personal + '/work/h5py/h5py').encode(), personal, checkout, hosted_runner=True))

    def test_windows_pefile_pin_satisfies_packager_metadata(self):
        from importlib.metadata import requires
        from packaging.requirements import Requirement
        pinned = next(Requirement(line) for line in (ROOT / 'packaging/requirements.lock.txt').read_text().splitlines()
                      if line.startswith('pefile=='))
        version = next(iter(pinned.specifier)).version
        for text in requires('pyinstaller'):
            dependency = Requirement(text)
            if dependency.name == 'pefile':
                self.assertIn(version, dependency.specifier)

    def test_windows_hosted_vendor_paths_preserve_private_and_checkout_rejection(self):
        audit = self.module('audit_payload')
        home = '\\'.join(('C:', 'Users', 'runneradmin'))
        checkout = r'D:\a\current\current'
        # Actual path contexts from the locked Windows wheels (three Rust modules,
        # upstream h5py/HDF5, and the byte-identical Blosc2 runtime).
        vendor = [r'\.cargo\registry\src\index.crates.io-1949cf8c6b5b557f\pyo3-0.29.0\src\instance.rs',
                  r'\.cargo\registry\src\index.crates.io-1949cf8c6b5b557f\url-2.5.8\src\parser.rs',
                  r'\.cargo\registry\src\index.crates.io-1949cf8c6b5b557f\lexical-parse-float-1.0.6\src\bigint.rs',
                  r'\AppData\Local\Temp\tmp453k4de2\hdf5-hdf5_2.0.0\src\H5.c',
                  r'\AppData\Local\Temp\tmpto43qb23\build\_deps\blosc2-src\plugins\codecs\ndlz\ndlz.c']
        for suffix in vendor:
            data = (home + suffix + '\0').encode()
            self.assertFalse(audit.contains_private_path(data, home, checkout, hosted_runner=True), suffix)
            self.assertTrue(audit.contains_private_path(data, home, checkout, hosted_runner=False))
            personal = home.replace('runneradmin', 'personal-user')
            self.assertTrue(audit.contains_private_path((personal + suffix).encode(), personal, checkout, hosted_runner=True))
        settings = ('SUMMARY OF THE HDF5 CONFIGURATION\nModule Directory: ' +
                    home.replace('\\', '/') + '/AppData/Local/Temp/tmpp83d3cae/mod\n').encode()
        self.assertFalse(audit.contains_private_path(settings, home, checkout, hosted_runner=True))
        for path in (checkout + r'\build\native-dependencies\sources\hdf5-1.14.6\src\H5.c',
                     checkout.replace('\\', '/') + '/build/native-dependencies/hdf5',
                     home + r'\.ssh\id_ed25519', home + r'\.cargo\credentials.toml',
                     home + r'\AppData\Local\Temp\secret.json',
                     home + r'\AppData\Local\Temp\tmpp83d3cae\mod',
                     home + r'\AppData\Local\Temp\tmp123\hdf5-hdf5_2.0.0\src\secret.json',
                     home + r'\AppData\Local\Temp\tmp123\build\_deps\blosc2-src\..\secret.c'):
            self.assertTrue(audit.contains_private_path(path.encode(), home, checkout, hosted_runner=True), path)

    def test_windows_hdf_diagnostic_mapping_preserves_line_numbers_and_source(self):
        build = self.module('build_native')
        self.assertTrue(hasattr(build, 'map_hdf_source_locations'))
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / 'hdf5/src/probe.c'
            source.parent.mkdir(parents=True)
            original = b'/* Copyright retained */\n#include <stdio.h>\nconst char *file = __FILE__;\nint main(void) { printf("%s:%d", file, __LINE__); }\n'
            source.write_bytes(original)
            self.assertEqual(build.map_hdf_source_locations(root / 'hdf5'), ['src/probe.c'])
            self.assertEqual(source.read_bytes(), b'#line 1 "src/probe.c"\n' + original)
            build.map_hdf_source_locations(root / 'hdf5')
            self.assertEqual(source.read_bytes().count(b'#line'), 1)
            # Exercise the standard C directive, not just generated source text.
            compiler = shutil.which('cl') if sys.platform == 'win32' else (shutil.which('cc') or shutil.which('gcc'))
            if compiler is not None:
                binary = root / ('probe.exe' if sys.platform == 'win32' else 'probe')
                args = ([compiler, '/nologo', str(source), '/Fe:' + str(binary), '/Fo:' + str(root / 'probe.obj')]
                        if sys.platform == 'win32' else [compiler, str(source), '-o', str(binary)])
                subprocess.run(args, check=True, cwd=root)
                self.assertEqual(subprocess.check_output([str(binary)], text=True), 'src/probe.c:4')

    def test_generated_windows_hdf_metadata_maps_both_path_spellings(self):
        build = self.module('build_native')
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'H5build_settings.c'
            prefix = r'D:\a\current\current'
            path.write_text('Copyright retained\n' + prefix + '\n' + prefix.replace('\\', '/') + '\n' +
                            prefix.replace('\\', '\\\\'), encoding='utf-8')
            build.map_build_metadata(path, [prefix])
            self.assertEqual(path.read_text(encoding='utf-8'), 'Copyright retained\n/shaq-build\n/shaq-build\n/shaq-build')

    def test_x64_sqlalchemy_runtime_dependencies_are_pinned(self):
        from importlib.metadata import requires
        from packaging.markers import default_environment
        from packaging.requirements import Requirement
        from packaging.utils import canonicalize_name
        lines = (ROOT / 'packaging/requirements.lock.txt').read_text().splitlines()
        for platform, machine in (('win32', 'AMD64'), ('darwin', 'x86_64')):
            environment = dict(default_environment(), sys_platform=platform, platform_machine=machine, extra='')
            pins = {}
            for line in lines:
                if line and not line.startswith('#'):
                    requirement = Requirement(line)
                    if requirement.marker is None or requirement.marker.evaluate(environment):
                        pins[canonicalize_name(requirement.name)] = next(iter(requirement.specifier)).version
            for text in requires('SQLAlchemy'):
                requirement = Requirement(text)
                if requirement.marker is None or requirement.marker.evaluate(environment):
                    self.assertIn(canonicalize_name(requirement.name), pins, text)
                    self.assertIn(pins[canonicalize_name(requirement.name)], requirement.specifier)

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
                    private.parent.relative_to(root).as_posix(), private.relative_to(root).as_posix()])

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

    def test_gui_smoke_fixture_overrides_remain_bound_api_methods(self):
        from shaq_daily_oracle import desktop

        class Bridge:
            def get_lab_state(self):
                raise AssertionError
            def get_shadow_batch(self, batch_id):
                raise AssertionError
            def refresh_prices_and_results(self):
                raise AssertionError

        bridge = Bridge()
        state = {"result_refresh": {"status": "idle"}}
        detail = {"labels": {"labels": {}}}
        desktop._bind_gui_smoke_fixture(bridge, state, detail)
        api = desktop.desktop_api(bridge)
        for name in ("get_lab_state", "get_shadow_batch", "refresh_prices_and_results"):
            self.assertTrue(inspect.ismethod(getattr(bridge, name)))
            self.assertTrue(inspect.ismethod(getattr(api, name)))
        self.assertTrue(api.get_lab_state()["ok"])
        self.assertTrue(api.get_shadow_batch("fixture")["ok"])
        self.assertEqual(api.refresh_prices_and_results()["value"]["status"], "complete")

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
