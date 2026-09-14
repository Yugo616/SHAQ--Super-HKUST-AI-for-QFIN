import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import sys
import unittest
import zipfile
from unittest.mock import patch

import httpx


ROOT = Path(__file__).resolve().parents[1]
REPO = 'Yugo616/SHAQ--Super-HKUST-AI-for-QFIN'
CHANNEL = 'osx-arm64-stable'


def module():
    path = ROOT / 'packaging/release_feed.py'
    if not path.is_file():
        raise AssertionError('Final packaging must prepare a verified public delta base')
    spec = importlib.util.spec_from_file_location('release_feed', path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def release(version, channel=CHANNEL):
    tag = f'lab-v{version}-macos'
    base = f'https://github.com/{REPO}/releases/download/{tag}/'
    filename = f'SHAQDailyOracleLab-{version}-{channel}-full.nupkg'
    asset = dict(PackageId='SHAQDailyOracleLab', Version=version, Type='Full',
                 FileName=filename, SHA256=hashlib.sha256(b'public full').hexdigest(), Size=11)
    feed_name = f'releases.{channel}.json'
    row = dict(tag_name=tag, html_url=f'https://github.com/{REPO}/releases/tag/{tag}',
               draft=False, prerelease=True, assets=[
                   dict(name=feed_name, browser_download_url=base+feed_name),
                   dict(name=filename, browser_download_url=base+filename, size=11)])
    return row, {base+feed_name: {'Assets': [asset]}, base+filename: b'public full'}


class ReleaseFeedTests(unittest.TestCase):
    def test_build_token_authenticates_only_release_listing_not_downloads_or_receipts(self):
        helper = module()
        row, responses = release('0.7.0')
        token = 'fixture-build-token'
        api = f'https://api.github.com/repos/{REPO}/releases'
        for configured in ('', token):
            with self.subTest(authenticated=bool(configured)), tempfile.TemporaryDirectory() as directory:
                output = Path(directory)/'feed'; calls = []
                def transport(request):
                    calls.append(str(request.url))
                    if str(request.url).split('?')[0] == api:
                        if request.headers.get('Authorization') != 'Bearer ' + token:
                            return httpx.Response(403, json={'message': 'API rate limit exceeded'})
                        return httpx.Response(200, json=[row])
                    self.assertNotIn('authorization', request.headers)
                    url = str(request.url)
                    if request.url.host == 'github.com':
                        return httpx.Response(302, headers={'Location': 'https://release-assets.githubusercontent.com/'+request.url.path.rsplit('/', 1)[-1]})
                    self.assertEqual(request.url.host, 'release-assets.githubusercontent.com')
                    value = next(value for url, value in responses.items() if url.endswith(request.url.path))
                    return httpx.Response(200, content=value) if isinstance(value, bytes) else httpx.Response(200, json=value)
                with patch.dict(os.environ, {'SHAQ_BUILD_GITHUB_TOKEN': configured}), \
                        httpx.Client(transport=httpx.MockTransport(transport), follow_redirects=True) as client:
                    if configured:
                        receipt = helper.prepare_public_base(ROOT, output, '0.8.0', 'Darwin', 'arm64', client=client)
                        self.assertEqual(receipt['status'], 'public-base')
                        self.assertEqual(len(calls), 5)
                        self.assertNotIn(token, json.dumps(receipt))
                        self.assertTrue(all(token.encode() not in path.read_bytes() for path in output.iterdir()))
                    else:
                        with self.assertRaises(httpx.HTTPStatusError) as raised:
                            helper.prepare_public_base(ROOT, output, '0.8.0', 'Darwin', 'arm64', client=client)
                        self.assertEqual(raised.exception.response.status_code, 403)
                        self.assertFalse(output.exists())
                    self.assertNotIn('authorization', client.headers)

    def test_authenticated_listing_redirect_is_rejected_even_with_redirect_enabled_client(self):
        helper = module()
        for destination in (f'https://api.github.com/repos/{REPO}/other', 'https://example.com/redirect'):
            with self.subTest(destination=destination), tempfile.TemporaryDirectory() as directory:
                calls = []
                def transport(request):
                    calls.append(str(request.url))
                    if len(calls) == 1:
                        return httpx.Response(302, headers={'Location': destination})
                    return httpx.Response(200, json=[])
                with patch.dict(os.environ, {'SHAQ_BUILD_GITHUB_TOKEN': 'fixture-build-token'}), \
                        httpx.Client(transport=httpx.MockTransport(transport), follow_redirects=True) as client:
                    with self.assertRaises(httpx.HTTPStatusError) as raised:
                        helper.prepare_public_base(ROOT, Path(directory)/'feed', '0.8.0', 'Darwin', 'arm64', client=client)
                    self.assertEqual(raised.exception.response.status_code, 302)
                self.assertEqual(len(calls), 1)
                self.assertFalse((Path(directory)/'feed').exists())

    def test_final_managed_cli_prepares_exact_pack_output_on_each_native_platform(self):
        spec = importlib.util.spec_from_file_location('build_desktop', ROOT/'packaging/build_desktop.py')
        build = importlib.util.module_from_spec(spec); spec.loader.exec_module(build)
        for system, machine, channel in [('Darwin', 'arm64', 'osx-arm64-stable'),
                                          ('Windows', 'AMD64', 'win-x64-stable')]:
            with self.subTest(system=system), tempfile.TemporaryDirectory() as directory:
                output = Path(directory)/'exact final pack'; payload = Path(directory)/'Payload.app'
                payload.mkdir()
                def pack(command, **kwargs):
                    packed = Path(command[command.index('--outputDir')+1])
                    self.assertEqual(packed, output.resolve())
                    self.assertEqual(json.loads((packed/f'delta-base.{channel}.json').read_text())['status'], 'first-managed-release')
                    filename = f'SHAQDailyOracleLab-0.8.0-{channel}-full.nupkg'
                    with zipfile.ZipFile(packed/filename, 'w') as archive: archive.writestr('lib/app', b'final')
                    data = (packed/filename).read_bytes()
                    (packed/f'releases.{channel}.json').write_text(json.dumps({'Assets': [dict(
                        PackageId='SHAQDailyOracleLab', Version='0.8.0', Type='Full', FileName=filename,
                        SHA256=hashlib.sha256(data).hexdigest(), Size=len(data))]}))
                    if system == 'Windows':
                        (packed/'native-Setup.exe').write_bytes(b'fixture installer')
                        (packed/f'assets.{channel}.json').write_text(json.dumps([
                            dict(Type='Installer', RelativeFileName='native-Setup.exe')]))
                client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[])))
                with patch.object(sys, 'argv', ['build_desktop', '--manage-existing', str(payload), '--output', str(output),
                                              '--version', '0.8.0', '--prepare-public-base']), \
                        patch.object(sys, 'path', [str(ROOT/'packaging'), *sys.path]), \
                        patch.object(build.platform, 'system', return_value=system), \
                        patch.object(build.platform, 'machine', return_value=machine), \
                        patch.object(build.sys, 'platform', 'win32' if system == 'Windows' else 'darwin'), \
                        patch.object(build.subprocess, 'check_output', return_value='Velopack CLI 1.2.0,'), \
                        patch.object(build.subprocess, 'run', pack), patch('httpx.Client', return_value=client):
                    build.main()
                feed = json.loads((output/f'releases.{channel}.json').read_text())
                self.assertRegex(feed['Assets'][0]['ContentSHA256'], r'^[0-9a-f]{64}$')
                if system == 'Windows':
                    setup_name = 'SHAQ-Daily-Oracle-Lab-Windows-x64-Setup.exe'
                    self.assertEqual((output/setup_name).read_bytes(), b'fixture installer')
                    self.assertEqual(json.loads((output/f'assets.{channel}.json').read_text()),
                                     [dict(Type='Installer', RelativeFileName=setup_name)])

    def prepare(self, rows, responses, target='0.8.0'):
        helper = module()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'final feed 中文'
            calls = []

            def transport(request):
                self.assertNotIn('authorization', request.headers)
                calls.append(str(request.url))
                if request.url.host == 'api.github.com':
                    return httpx.Response(200, json=rows)
                value = responses[str(request.url)]
                if isinstance(value, Exception):
                    raise value
                if isinstance(value, int):
                    return httpx.Response(value)
                return httpx.Response(200, content=value) if isinstance(value, bytes) else httpx.Response(200, json=value)

            with httpx.Client(transport=httpx.MockTransport(transport)) as client:
                receipt = helper.prepare_public_base(ROOT, output, target, 'Darwin', 'arm64', client=client)
            files = {p.name: p.read_bytes() for p in output.iterdir()}
            self.assertEqual(json.loads(files['delta-base.osx-arm64-stable.json']), receipt)
            return receipt, files, calls

    def test_first_managed_release_writes_truthful_full_only_receipt(self):
        legacy, _ = release('0.6.2'); legacy['assets'] = []
        receipt, files, _ = self.prepare([legacy], {})
        self.assertEqual(receipt['status'], 'first-managed-release')
        self.assertEqual(set(files), {'delta-base.osx-arm64-stable.json'})

    def test_latest_lower_same_arch_public_feed_is_staged_not_acceptance_versions(self):
        rows = []; responses = {}
        for version, arch in [('0.7.0', CHANNEL), ('0.6.98', CHANNEL), ('0.6.99', CHANNEL),
                              ('0.8.0', CHANNEL), ('0.9.0', CHANNEL), ('0.7.1', 'osx-x64-stable')]:
            row, mapping = release(version, arch); rows.append(row); responses.update(mapping)
        receipt, files, calls = self.prepare(rows, responses)
        self.assertEqual(receipt['status'], 'public-base')
        self.assertEqual(receipt['base_version'], '0.7.0')
        self.assertEqual(files['SHAQDailyOracleLab-0.7.0-osx-arm64-stable-full.nupkg'], b'public full')
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(json.loads(files['releases.osx-arm64-stable.json'])['Assets']), 1)

    def test_wrong_arch_digest_and_size_fail_without_full_only_fallback(self):
        for field, value in [('FileName', 'SHAQDailyOracleLab-0.7.0-osx-x64-stable-full.nupkg'),
                             ('SHA256', '0'*64), ('Size', 12)]:
            row, responses = release('0.7.0')
            next(v for v in responses.values() if isinstance(v, dict))['Assets'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.prepare([row], responses)

    def test_advertised_feed_network_errors_are_not_first_release_absence(self):
        for fault in (404, 503, httpx.ConnectError('network unavailable')):
            row, responses = release('0.7.0')
            responses[next(iter(responses))] = fault
            with self.subTest(fault=fault), self.assertRaises(httpx.HTTPError):
                self.prepare([row], responses)

    def test_arbitrary_feed_or_package_download_urls_are_rejected(self):
        for index in (0, 1):
            row, responses = release('0.7.0')
            row['assets'][index]['browser_download_url'] = 'https://example.com/stolen'
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.prepare([row], responses)

    def test_release_list_failure_is_not_empty_release_history(self):
        helper = module()
        with tempfile.TemporaryDirectory() as directory, httpx.Client(transport=httpx.MockTransport(
                lambda request: httpx.Response(403))) as client:
            with self.assertRaises(httpx.HTTPStatusError):
                helper.prepare_public_base(ROOT, Path(directory)/'feed', '0.8.0', 'Darwin', 'arm64', client=client)
            self.assertFalse((Path(directory)/'feed/delta-base.osx-arm64-stable.json').exists())

    def test_dirty_output_is_rejected_without_removing_existing_packages(self):
        helper = module()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory); previous = output/'existing.nupkg'; previous.write_bytes(b'keep')
            with self.assertRaises(ValueError):
                helper.prepare_public_base(ROOT, output, '0.8.0', 'Darwin', 'arm64')
            self.assertEqual(previous.read_bytes(), b'keep')
