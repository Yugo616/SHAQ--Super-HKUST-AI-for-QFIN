import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class DmgCreationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('zsh'), 'macOS final shell requires zsh')
    def test_final_shell_invokes_shared_dmg_helper_and_public_base_in_exact_feed(self):
        with tempfile.TemporaryDirectory(prefix='final 中文 space-') as directory:
            root = Path(directory).resolve(); (root/'packaging').mkdir(); (root/'dist').mkdir()
            shutil.copy2(ROOT/'packaging/build_macos.sh', root/'packaging/build_macos.sh')
            fake = root/'build-python'
            fake.write_text('''#!'''+sys.executable+'''
import json, os, pathlib, sys
args=sys.argv[1:]; root=pathlib.Path(os.environ['TEST_BUILD_ROOT'])
with (root/'calls.jsonl').open('a') as stream: stream.write(json.dumps(args)+'\\n')
if args[0].endswith('build_desktop.py') and '--manage-existing' not in args:
    output=pathlib.Path(args[args.index('--output')+1]); name=args[args.index('--name')+1]
    executable=output/(name+'.app')/'Contents/MacOS'/name
    executable.parent.mkdir(parents=True); executable.write_text('#!/bin/sh\\nexit 0\\n'); executable.chmod(0o755)
if args[0].endswith('create_dmg.py'):
    pathlib.Path(args[args.index('--output')+1]).write_bytes(b'fixture DMG')
''')
            fake.chmod(0o755)
            shell = '''function /usr/bin/codesign() { return 0; }
function /usr/bin/xattr() { return 0; }
function ditto() {
  if [[ "$1" == -xk ]]; then mkdir -p "$3/SHAQ Daily Oracle Lab.app";
  else /bin/cp -R "$3" "$4"; fi
}
source "$0"
'''
            env = dict(os.environ, TEST_BUILD_ROOT=str(root), SHAQ_BUILD_PYTHON=str(fake),
                       SHAQ_BUILD_VERSION='0.7.0', SHAQ_OUTPUT_ROOT=str(root/'native'),
                       SHAQ_FEED_ROOT=str(root/'final feed'), TMPDIR=str(root), SHAQ_PREVIEW_ONLY='0')
            result = subprocess.run(['zsh', '-c', shell, str(root/'packaging/build_macos.sh')], env=env,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = [json.loads(line) for line in (root/'calls.jsonl').read_text().splitlines()]
            pack = next(args for args in calls if '--manage-existing' in args)
            self.assertIn('--prepare-public-base', pack)
            self.assertEqual(pack[pack.index('--output')+1], str(root/'final feed'))
            dmg = next(args for args in calls if args[0] == 'packaging/create_dmg.py')
            self.assertEqual(dmg[dmg.index('--volume')+1], 'SHAQ Daily Oracle Lab')
            self.assertEqual(Path(dmg[dmg.index('--output')+1]).read_bytes(), b'fixture DMG')

    def test_bridge_acceptance_routes_creation_through_same_helper(self):
        spec = importlib.util.spec_from_file_location('installed_update_acceptance', ROOT/'packaging/installed_update_acceptance.py')
        acceptance = importlib.util.module_from_spec(spec); spec.loader.exec_module(acceptance)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); project = root/'project'; (project/'packaging').mkdir(parents=True)
            shutil.copytree(ROOT/'config', project/'config')
            shutil.copy2(ROOT/'packaging/updater-toolchain.json', project/'packaging/updater-toolchain.json')
            work = root/'isolated'; work.mkdir()
            commands = []
            def native(args, **kwargs):
                commands.append(args)
                if '--manage-existing' in args:
                    feed = Path(args[args.index('--output')+1]); feed.mkdir(exist_ok=True)
                    (feed/'SHAQDailyOracleLab-osx-arm64-stable-Portable.zip').write_bytes(b'fixture')
                # Stop before mounts/installation, after checking actual bridge command routing.
                code = 17 if Path(kwargs['stdout'].name).name == 'bridge-dmg.log' else 0
                return subprocess.CompletedProcess(args, code)
            with patch.object(acceptance, '__file__', str(project/'packaging/installed_update_acceptance.py')), \
                    patch.object(sys, 'argv', ['installed_update_acceptance']), patch.object(sys, 'platform', 'darwin'), \
                    patch.object(acceptance.platform, 'system', return_value='Darwin'), \
                    patch.object(acceptance.platform, 'machine', return_value='arm64'), \
                    patch.object(acceptance.tempfile, 'mkdtemp', return_value=str(work)), \
                    patch.object(acceptance.subprocess, 'run', native):
                self.assertEqual(acceptance.main(), 2)
            command = commands[-1]
            self.assertEqual(command[:2], [sys.executable, str(project/'packaging/create_dmg.py')])
            self.assertEqual(command[command.index('--source')+1], str(work/'dmg-source'))
            self.assertEqual(command[command.index('--output')+1], str(work/'bridge.dmg'))

    def helper(self):
        path = ROOT / 'packaging/create_dmg.py'
        self.assertTrue(path.is_file(), 'Both DMG callers need the bounded native creation helper')
        spec = importlib.util.spec_from_file_location('create_dmg', path)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        return helper

    def exercise(self, outcomes):
        helper = self.helper()
        with tempfile.TemporaryDirectory(prefix='dmg 中文 space-') as name:
            root = Path(name).resolve()
            source = root / 'source 中文'; source.mkdir()
            output = root / 'existing image.dmg'; output.write_bytes(b'previous accepted')
            commands = []

            def native(args, **kwargs):
                commands.append(args)
                code, message = outcomes[min(len(commands)-1, len(outcomes)-1)]
                Path(args[-1]).write_bytes(b'partial' if code else b'new accepted')
                return subprocess.CompletedProcess(args, code, stdout=message)

            capture = io.StringIO()
            with patch.object(helper.subprocess, 'run', native), patch.object(helper.time, 'sleep') as sleep, contextlib.redirect_stdout(capture):
                status = helper.create_dmg(source, output, 'Volume 中文 space', attempts=3, backoff_seconds=0)
            self.assertTrue(all(cmd[cmd.index('-srcfolder')+1] == str(source) for cmd in commands))
            self.assertTrue(all(cmd[cmd.index('-volname')+1] == 'Volume 中文 space' for cmd in commands))
            self.assertTrue(all(Path(cmd[-1]).parent.parent == root and Path(cmd[-1]) != output for cmd in commands))
            logs = list(root.glob('*.create-*/attempt-*.log'))
            self.assertEqual(len(logs), len(commands))
            self.assertTrue(all(message in capture.getvalue() for _, message in outcomes))
            return status, output.read_bytes(), len(commands), sleep.call_count

    def test_busy_then_success_promotes_only_success_and_retains_every_attempt(self):
        self.assertEqual(self.exercise([(1, 'hdiutil: create failed - Resource busy\n'), (0, 'created successfully\n')]),
                         (0, b'new accepted', 2, 1))

    def test_busy_exhaustion_retains_previous_output_and_original_status(self):
        self.assertEqual(self.exercise([(1, 'hdiutil: create failed - Resource busy\n')]),
                         (1, b'previous accepted', 3, 2))

    def test_nonbusy_errors_do_not_retry_even_with_status_one(self):
        for code, message in ((1, 'hdiutil: create failed - Permission denied\n'),
                              (22, 'hdiutil: invalid argument\n'),
                              (1, 'unrelated Resource busy\n')):
            with self.subTest(message=message):
                self.assertEqual(self.exercise([(code, message)]), (code, b'previous accepted', 1, 0))

    def test_timeout_retains_partial_diagnostics_and_previous_output(self):
        helper = self.helper()
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve(); source = root/'source'; source.mkdir()
            output = root/'final.dmg'; output.write_bytes(b'keep')
            with patch.object(helper.subprocess, 'run', side_effect=subprocess.TimeoutExpired(
                    ['hdiutil', 'create'], 1, output=b'partial native diagnostic\n')):
                with self.assertRaises(subprocess.TimeoutExpired):
                    helper.create_dmg(source, output, 'fixture', timeout_seconds=1)
            logs = list(root.glob('*.create-*/attempt-*.log'))
            self.assertEqual(len(logs), 1)
            self.assertIn('partial native diagnostic', logs[0].read_text())
            self.assertEqual(output.read_bytes(), b'keep')
