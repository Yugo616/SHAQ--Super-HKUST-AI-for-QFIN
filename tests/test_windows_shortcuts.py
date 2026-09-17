import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ShortcutPlatformTests(unittest.TestCase):
    @unittest.skipIf(sys.platform == 'win32', 'non-Windows guard')
    def test_other_platforms_never_touch_shell_links(self):
        from shaq_daily_oracle.windows_shortcuts import repair_shortcuts
        self.assertEqual(repair_shortcuts(Path('/not/a/real/app')), {'repaired': 0})


@unittest.skipUnless(sys.platform == 'win32', 'Windows shell integration')
class WindowsShortcutTests(unittest.TestCase):
    def test_repair_existing_links_uses_current_install_and_preserves_other_apps(self):
        from shaq_daily_oracle.windows_shortcuts import repair_shortcuts
        with tempfile.TemporaryDirectory(prefix='shortcut 中文 ') as tmp:
            root=Path(tmp); exe=root/'current'/'Example Lab.exe'
            exe.parent.mkdir(); exe.write_bytes(b'fixture')
            links=root/'links'; links.mkdir()
            env=dict(os.environ, TEST_LINK=str(links/'Example Lab.lnk'),
                     TEST_OTHER=str(links/'Other.lnk'), TEST_MISSING=str(root/'other-user'/'Example Lab.exe'))
            subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',
                '$w=New-Object -ComObject WScript.Shell; '
                '$s=$w.CreateShortcut($env:TEST_LINK);$s.TargetPath=$env:TEST_MISSING;$s.Save(); '
                '$s=$w.CreateShortcut($env:TEST_OTHER);$s.TargetPath=$env:TEST_MISSING;$s.Save()'],
                env=env,check=True,capture_output=True)
            before=(links/'Other.lnk').read_bytes()
            self.assertEqual(repair_shortcuts(exe,[links])['repaired'],1)
            self.assertEqual(repair_shortcuts(exe,[links])['repaired'],0)
            self.assertEqual((links/'Other.lnk').read_bytes(),before)
            actual=subprocess.check_output(['powershell.exe','-NoProfile','-NonInteractive','-Command',
                '$w=New-Object -ComObject WScript.Shell; $w.CreateShortcut($env:TEST_LINK).TargetPath'],env=env,text=True).strip()
            self.assertEqual(Path(actual),exe)
