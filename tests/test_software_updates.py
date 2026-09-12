import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from shaq_daily_oracle import software_updates as updates

ROOT=Path(__file__).parents[1]
REPO='team/project'


def release(version='0.6.2',suffix='macos',asset='SHAQ-Daily-Oracle-Lab-macOS-Apple-Silicon.dmg'):
    tag=f'lab-v{version}-{suffix}'
    return {'tag_name':tag,'draft':False,'prerelease':False,'body':'Fixes',
            'html_url':f'https://github.com/{REPO}/releases/tag/{tag}',
            'assets':[{'name':asset,'size':100,'browser_download_url':f'https://github.com/{REPO}/releases/download/{tag}/{asset}'}]}


class SoftwareUpdateTests(unittest.TestCase):
    def test_correct_architecture_and_no_downgrade(self):
        rows=[release(),release('0.7.0',asset='SHAQ-Daily-Oracle-Lab-macOS-Intel.dmg')]
        result=updates.select_release(rows,current='0.6.1',repository=REPO,system='Darwin',machine='arm64')
        self.assertEqual(result['latest_version'],'0.6.2')
        self.assertEqual(result['status'],'available')
        self.assertEqual(result['mode'],'installer_only')
        self.assertEqual(updates.select_release(rows,current='0.8.0',repository=REPO,
                         system='Darwin',machine='arm64')['status'],'local_newer')

    def test_windows_and_intel_are_separate(self):
        rows=[release('0.6.2','windows','SHAQ-Daily-Oracle-Lab-Windows-x64-Setup.exe'),
              release(asset='SHAQ-Daily-Oracle-Lab-macOS-Intel.dmg')]
        result=updates.select_release(rows,current='0.6.1',repository=REPO,system='Windows',machine='AMD64')
        self.assertTrue(result['asset_url'].endswith('x64-Setup.exe'))
        self.assertEqual(updates.select_release(rows,current='0.6.1',repository=REPO,
                         system='Linux',machine='x86_64')['status'],'unsupported')

    def test_unpublished_wrong_repo_and_missing_arch_assets_are_ignored(self):
        for mutation in ('draft','html_url','asset'):
            row=release()
            if mutation in ('draft','prerelease'):row[mutation]=True
            elif mutation=='html_url':row['html_url']='https://evil.example/download'
            else:row['assets'][0]['browser_download_url']='https://github.com/other/repo/evil'
            self.assertEqual(updates.select_release([row],current='0.6.1',repository=REPO,
                             system='Darwin',machine='arm64')['status'],'no_release')

    def test_lab_internal_test_releases_are_shown_with_their_label(self):
        row=release();row['prerelease']=True
        result=updates.select_release([row],current='0.6.1',repository=REPO,
                                      system='Darwin',machine='arm64')
        self.assertEqual(result['status'],'available')
        self.assertTrue(result['internal_test_release'])

    def test_project_version_wins_over_stale_distribution(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'pyproject.toml').write_text('[project]\nversion="0.6.2"\n',encoding='utf-8')
            self.assertEqual(updates.application_version(root),'0.6.2')

    def test_native_bundle_identity_uses_project_version(self):
        spec=importlib.util.spec_from_file_location('native_build',ROOT/'packaging/build_desktop.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);app=root/'Test.app';(app/'Contents').mkdir(parents=True)
            import plistlib
            (root/'pyproject.toml').write_text('[project]\nversion="0.6.2"\n',encoding='utf-8')
            path=app/'Contents/Info.plist';path.write_bytes(plistlib.dumps({'CFBundleName':'Test'}))
            module.set_macos_bundle_identity(root,app)
            value=plistlib.loads(path.read_bytes())
            self.assertEqual(value['CFBundleVersion'],'0.6.2')
            self.assertEqual(value['CFBundleShortVersionString'],'0.6.2')
            self.assertEqual(value['CFBundleIdentifier'],'io.shaq.dailyoracle.lab')
            (root/'pyproject.toml').write_text('[project]\nversion="0.6.2.dev3"\n',encoding='utf-8')
            with self.assertRaises(ValueError):module.set_macos_bundle_identity(root,app)
