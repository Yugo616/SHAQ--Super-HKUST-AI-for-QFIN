"""One payload recipe for the release and preview, on the actual host architecture."""
from pathlib import Path
import argparse
import base64
import csv
import hashlib
import importlib.metadata as metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
import plistlib
import re
import tomllib


def verified_wheel_files(archive, expected_sha256, installed):
    """Prove unchanged installed files against a published archive and its RECORD."""
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected_sha256:
        raise ValueError(f'Upstream wheel archive digest mismatch: {archive.name}')
    verified = {}
    with zipfile.ZipFile(archive) as wheel:
        record = next(name for name in wheel.namelist() if name.endswith('.dist-info/RECORD'))
        for name, recorded, size in csv.reader(wheel.read(record).decode('utf-8').splitlines()):
            if name not in installed or not recorded.startswith('sha256='):
                continue
            data = wheel.read(name)
            digest = hashlib.sha256(data).digest()
            if (recorded != 'sha256=' + base64.urlsafe_b64encode(digest).decode().rstrip('=') or
                    size != str(len(data))):
                raise ValueError(f'Upstream wheel RECORD mismatch: {name}')
            if data == installed[name]:
                verified[name] = digest.hex()
    return verified


def collect_upstream_path_provenance(destination):
    """Only hosted Windows source strings need extra original-wheel evidence."""
    proofs = []
    if (sys.platform == 'win32' and os.environ.get('GITHUB_ACTIONS') == 'true' and
            os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted'):
        from packaging.utils import parse_wheel_filename
        cache = destination.parent / 'upstream-wheels'
        cache.mkdir(exist_ok=True)
        home = str(Path.home()).replace('\\', '/').lower().encode()
        for dist in metadata.distributions():
            # Locally rebuilt/repaired components cannot inherit an upstream exemption.
            if dist.read_text('direct_url.json') or dist.metadata['Name'].lower() == 'shaq-daily-oracle':
                continue
            installed = {}
            for item in dist.files or []:
                if item.suffix.lower() not in ('.pyd', '.dll', '.c', '.h'):
                    continue
                path = Path(dist.locate_file(item))
                if path.is_file():
                    data = path.read_bytes()
                    if home in data.replace(b'\\', b'/').lower():
                        installed[item.as_posix()] = data
            if not installed:
                continue
            tags = {line[5:] for line in (dist.read_text('WHEEL') or '').splitlines() if line.startswith('Tag: ')}
            with urllib.request.urlopen(f'https://pypi.org/pypi/{dist.metadata["Name"]}/{dist.version}/json', timeout=60) as response:
                releases = json.load(response)['urls']
            candidates = [release for release in releases if release['packagetype'] == 'bdist_wheel' and
                          tags & {str(tag) for tag in parse_wheel_filename(release['filename'])[3]}]
            if len(candidates) != 1:
                raise ValueError(f'Ambiguous original wheel for {dist.metadata["Name"]} {dist.version}')
            release = candidates[0]
            archive = cache / release['filename']
            if not archive.is_file():
                with urllib.request.urlopen(release['url'], timeout=60) as response, archive.open('wb') as output:
                    shutil.copyfileobj(response, output)
            sha = release['digests']['sha256']
            files = verified_wheel_files(archive, sha, installed)
            proofs.append({'distribution': dist.metadata['Name'], 'version': dist.version,
                           'wheel_url': release['url'], 'wheel_sha256': sha, 'files': files})
    (destination / 'upstream-path-provenance.json').write_text(json.dumps(proofs, indent=2), encoding='utf-8')


def reviewed_notice(destination, name, version):
    directory = Path(__file__).parent / 'licenses'
    record = json.loads((directory / 'reviewed-sources.json').read_text(encoding='utf-8'))[name]
    if record['version'] != version:
        raise RuntimeError('Reviewed license version mismatch')
    content = (directory / record['file']).read_bytes()
    if hashlib.sha256(content).hexdigest() != record['sha256']:
        raise RuntimeError('Reviewed license digest mismatch')
    target = destination / name / record['file']
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return [str(target.relative_to(destination))], record


def source_notices(destination, name, version=None, source=None):
    if source is None:
        with urllib.request.urlopen(f'https://pypi.org/pypi/{name}/{version}/json') as response:
            releases = json.load(response)['urls']
        candidate = next(item for item in releases if item['packagetype'] == 'sdist')
        source = {'url': candidate['url'], 'sha256': candidate['digests']['sha256']}
    archives = destination.parent / 'license-sources'
    archives.mkdir(exist_ok=True)
    archive = archives / source['url'].rsplit('/', 1)[-1]
    if not archive.is_file():
        urllib.request.urlretrieve(source['url'], archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != source['sha256']:
        raise RuntimeError(f'Source digest mismatch: {archive.name}')
    notices = []
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            path = Path(member.name)
            if member.isfile() and path.name.lower().startswith(('license', 'copying', 'notice')):
                content = tar.extractfile(member).read()
                if b'\x00' in content or '..' in path.parts or path.is_absolute():
                    continue
                target = destination / name / 'upstream' / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                notices.append(str(target.relative_to(destination)))
    if not notices and name == 'iso4217':
        # Upstream distributes a public-domain dedication in PKG-INFO, not a LICENSE file.
        with tarfile.open(archive) as tar:
            member = next(m for m in tar.getmembers() if m.name.count('/') == 1 and m.name.endswith('/PKG-INFO'))
            content = tar.extractfile(member).read()
            if b'License: Public Domain' not in content:
                raise RuntimeError('ISO4217 public-domain dedication changed')
            target = destination / name / 'PUBLIC-DOMAIN-DEDICATION.txt'
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(content)
            notices.append(str(target.relative_to(destination)))
    if not notices and name == 'proxy_tools':
        target = destination / name / 'LICENSE.txt'
        target.parent.mkdir(exist_ok=True)
        shutil.copy2(Path(__file__).parent / 'licenses/proxy_tools-LICENSE.txt', target)
        notices.append(str(target.relative_to(destination)))
    if not notices and name == 'velopack':
        notices, license_source = reviewed_notice(destination, name, version)
        source = {**source, 'reviewed_license': license_source}
    if not notices:
        raise RuntimeError(f'No license text found in {archive.name}; review before packaging')
    return notices, source


def stage_macos_blosc2(tables_package, destination):
    """Give the repaired library PyTables' loader filename before binary analysis."""
    libraries = sorted((tables_package / '.dylibs').glob('libblosc2.*.dylib'))
    if len(libraries) != 1:
        raise RuntimeError(f'Expected one repaired Blosc2 library, found {len(libraries)}')
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / 'libblosc2.dylib'
    shutil.copy2(libraries[0], target)
    return target


def stage_version(root, version):
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Build version must be a final numeric triplet')
    text = (root / 'pyproject.toml').read_text(encoding='utf-8')
    original = tomllib.loads(text)['project']['version']
    text, count = re.subn(r'(?m)^(version\s*=\s*["\'])' + re.escape(original) + r'(["\'])',
                          lambda match: match[1] + version + match[2], text)
    if count != 1:
        raise ValueError('Ambiguous project version')
    target = root / 'build/version-metadata/pyproject.toml'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding='utf-8')
    return target


def pyinstaller_args(root, output, name, blosc2_library=None, project_metadata=None):
    args = ['--noconfirm', '--clean', '--windowed', '--onedir', '--name', name,
            '--paths', str(root / 'src'), '--distpath', str(output),
            '--runtime-hook', str(root / 'packaging/frozen_native.py'),
            '--workpath', str(root / 'build/desktop-native'), '--specpath', str(root / 'build')]
    if sys.platform == 'darwin':
        args += ['--strip', '--osx-bundle-identifier', 'io.shaq.dailyoracle.lab']
    if blosc2_library is not None:
        args += ['--add-binary', f'{blosc2_library}:tables']
    args += ['--collect-submodules', 'scipy._external']
    for package in ('webview', 'zipline', 'tables', 'bcolz', 'pandas_market_calendars',
                    'exchange_calendars', 'yfinance', 'keyring', 'velopack'):
        args += ['--collect-all', package]
    for package in ('openai', 'quickjs', '_quickjs', 'keyring.backends.macOS' if sys.platform == 'darwin' else 'keyring.backends.Windows'):
        args += ['--hidden-import', package]
    for package in ('rfc3987_syntax', 'iso4217', 'shaq_daily_oracle'):
        args += ['--collect-data', package]
    for resource in ('config', 'governance', 'schemas', 'skills', 'decision', 'bundled_versions'):
        args += ['--add-data', f'{root / resource}:{resource}']
    args += ['--add-data', f'{project_metadata or root / "pyproject.toml"}:.',
             '--add-data', f'{root / "src/shaq_daily_oracle/desktop"}:shaq_daily_oracle/desktop',
             '--add-data', f'{root / "build/third-party"}:third-party',
             str(root / 'packaging/desktop_entry.py')]
    return args


def set_macos_bundle_identity(root, app, version=None):
    version = version or str(tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version'])
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Native bundle version must be a final numeric project triplet')
    path = app / 'Contents/Info.plist'
    data = plistlib.loads(path.read_bytes())
    data.update(CFBundleVersion=version, CFBundleShortVersionString=version,
                CFBundleIdentifier='io.shaq.dailyoracle.lab')
    path.write_bytes(plistlib.dumps(data))


def collect_notices(root, version_override=None):
    destination = root / 'build/third-party'
    if destination.is_dir():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    collect_upstream_path_provenance(destination)
    distributions = []
    sources = {}
    for dist in sorted(metadata.distributions(), key=lambda d: d.metadata['Name'].lower()):
        name = dist.metadata['Name']
        if name.lower() == 'shaq-daily-oracle':
            continue
        if name.lower() == 'backtrader':
            raise RuntimeError('Backtrader must not be installed in the build environment')
        target = destination / name
        target.mkdir(exist_ok=True)
        notices = []
        for item in dist.files or []:
            if (any(item.name.lower().startswith(term) for term in ('license', 'copying', 'notice'))
                    and not any(part.endswith('.dSYM') for part in item.parts)
                    and item.suffix.lower() not in ('.py', '.pyc', '.so', '.dylib', '.pyd', '.dll')):
                source = Path(dist.locate_file(item))
                if source.is_file() and b'\x00' not in source.read_bytes():
                    relative = Path(*[part for part in item.parts if part not in ('..', '.')])
                    path = target / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, path)
                    notices.append(str(path.relative_to(destination)))
        (target / 'METADATA.txt').write_text(dist.read_text('METADATA') or dist.read_text('PKG-INFO') or '', encoding='utf-8')
        if not notices:
            notices, sources[name] = source_notices(destination, name, dist.version)
        distributions.append({'name': name, 'version': dist.version, 'notices': notices})
    for name, source in json.loads((root / 'packaging/native-sources.json').read_text(encoding="utf-8")).items():
        _, sources[name] = source_notices(destination, name, source=source)
    if sys.platform == 'win32':
        provenance = json.loads((root / 'build/native-dependencies/mingw-toolchain.json').read_text(encoding='utf-8'))
        collect_mingw_notices(destination, provenance)
        shutil.copy2(root / 'build/native-dependencies/hdf5-diagnostic-map.json', destination / 'hdf5/diagnostic-map.json')
    (destination / 'source-manifest.json').write_text(json.dumps(sources, indent=2), encoding='utf-8')
    python_license = Path(sys.base_prefix) / 'Resources/Python.app/Contents/Resources/English.lproj/Documentation/License.html'
    candidates = [python_license, Path(sys.base_prefix) / 'LICENSE.txt', Path(sys.base_prefix) / 'lib/python3.13/LICENSE.txt']
    for candidate in candidates:
        if candidate.is_file():
            shutil.copy2(candidate, destination / ('Python-' + candidate.name))
            break
    shutil.copy2(root / 'docs/third-party-notices.md', destination / 'README.md')
    methods = {str(path.relative_to(root / 'bundled_versions')): hashlib.sha256(path.read_bytes()).hexdigest()
               for path in sorted((root / 'bundled_versions').rglob('*')) if path.is_file()}
    manifest = {'source_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(),
                'source_dirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=root, text=True).strip()),
                'python': platform.python_version(), 'architecture': platform.machine(),
                'methods': methods, 'distributions': distributions,
                'version_override': version_override,
                'source_version': tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']}
    (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')


def mingw_toolchain():
    """Bind QuickJS and its notices to the configured MSYS2 MINGW64 installation."""
    configured = os.environ.get('SHAQ_MINGW_PREFIX')
    if not configured:
        raise RuntimeError('Set SHAQ_MINGW_PREFIX to the MSYS2 MINGW64 prefix before building')
    prefix = Path(configured).resolve()
    compiler = prefix / 'bin/gcc.exe'
    if not compiler.is_file():
        raise RuntimeError(f'Missing configured MinGW compiler: {compiler}')
    required = {'gcc-libs': ('COPYING3', 'COPYING.LIB', 'COPYING.RUNTIME'),
                'crt': ('COPYING', 'COPYING.MinGW-w64.txt', 'COPYING.MinGW-w64-runtime.txt'),
                'headers': ('COPYING', 'COPYING.MinGW-w64.txt', 'COPYING.MinGW-w64-runtime.txt'),
                'winpthreads': ('COPYING',), 'libwinpthread': ('COPYING',)}
    for component, names in required.items():
        for name in names:
            notice = prefix / 'share/licenses' / component / name
            if not notice.is_file() or not notice.read_bytes().strip():
                raise RuntimeError(f'MinGW-W64 runtime license is required: {notice}')
    packages = {}
    database = prefix.parent / 'var/lib/pacman/local'
    for component in ('gcc', *required):
        name = 'mingw-w64-x86_64-' + component
        matches = []
        for desc in database.glob(name + '-*/desc'):
            fields = dict(block.split('\n', 1) for block in desc.read_text(encoding='utf-8').strip().split('\n\n'))
            if fields.get('%NAME%') == name:
                matches.append(fields)
        if len(matches) != 1 or not all(matches[0].get(key) for key in ('%VERSION%', '%BASE%')):
            raise RuntimeError(f'Missing or ambiguous MSYS2 package provenance: {name}')
        version, base = matches[0]['%VERSION%'], matches[0]['%BASE%']
        packages[name] = {'version': version, 'base': base,
                          'source_url': f'https://repo.msys2.org/mingw/sources/{base}-{version}.src.tar.zst'}
    provenance = {'compiler_sha256': hashlib.sha256(compiler.read_bytes()).hexdigest(), 'packages': packages}
    env = dict(os.environ)
    env['PATH'] = str(prefix / 'bin') + os.pathsep + env.get('PATH', '')
    return prefix, env, provenance


def collect_mingw_notices(destination, built_provenance):
    prefix, _, provenance = mingw_toolchain()
    if provenance != built_provenance:
        raise RuntimeError('MinGW toolchain changed since the native wheels were built')
    target = destination / 'MinGW-W64'
    shutil.copytree(prefix / 'share/licenses', target, dirs_exist_ok=True)
    (target / 'toolchain.json').write_text(json.dumps(provenance, indent=2), encoding='utf-8')


def managed_pack_args(root, payload, output, version, system, machine):
    key = system + '/' + machine.lower()
    tools = json.loads((root / 'packaging/updater-toolchain.json').read_text())
    updates = json.loads((root / 'config/software-updates.json').read_text())
    if key not in tools['runtimes'] or key not in updates['channels']:
        raise ValueError('Unsupported native update architecture')
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Invalid managed package version')
    main = payload.stem if system == 'Darwin' else payload.name + '.exe'
    args = ['pack', '--packId', updates['package_id'], '--packVersion', version,
            '--packDir', str(payload), '--mainExe', main, '--packTitle', payload.stem,
            '--runtime', tools['runtimes'][key], '--channel', updates['channels'][key],
            '--outputDir', str(output)]
    if system == 'Darwin':
        args += ['--noInst', 'true', '--signAppIdentity', '-']
    else:
        args += ['--framework', 'webview2']
    return args


def normalize_setup(output, channel):
    assets = json.loads((output/('assets.'+channel+'.json')).read_text())
    installers = [asset['RelativeFileName'] for asset in assets if asset['Type']=='Installer']
    if len(installers)!=1 or Path(installers[0]).name!=installers[0]:
        raise RuntimeError('Expected exactly one native Setup.exe')
    name='SHAQ-Daily-Oracle-Lab-Windows-x64-Setup.exe'
    (output/installers[0]).replace(output/name)
    for asset in assets:
        if asset['RelativeFileName']==installers[0]:asset['RelativeFileName']=name
    (output/('assets.'+channel+'.json')).write_text(json.dumps(assets),encoding='utf-8')


def pack_managed(root, payload, output, version):
    config = json.loads((root / 'packaging/updater-toolchain.json').read_text())
    tool = os.environ.get('SHAQ_VPK', 'vpk')
    help_text = subprocess.check_output([tool, '--help'], text=True)
    actual = re.search(r'Velopack CLI (\d+\.\d+\.\d+),', help_text)
    if not actual or actual[1] != config['velopack_version']:
        raise RuntimeError('Pinned Velopack tool version mismatch')
    subprocess.run([tool, *managed_pack_args(root, payload, output, version, platform.system(), platform.machine())], check=True)
    channel = json.loads((root / 'config/software-updates.json').read_text())['channels'][platform.system()+'/'+platform.machine().lower()]
    feed = json.loads((output / ('releases.' + channel + '.json')).read_text())
    from shaq_daily_oracle.software_updates import UpdateRuntime
    from shaq_daily_oracle.update_native import package_content_sha256
    package_id = json.loads((root / 'config/software-updates.json').read_text())['package_id']
    for asset in feed['Assets']:
        UpdateRuntime._validate_asset(asset, package_id, channel, version)
        file = output / asset['FileName']
        with file.open('rb') as stream:digest=hashlib.file_digest(stream, 'sha256').hexdigest()
        if file.stat().st_size != asset['Size'] or digest.lower() != asset['SHA256'].lower():
            raise RuntimeError('Generated native feed failed package digest verification')
        if asset['Type']=='Full':asset['ContentSHA256']=package_content_sha256(file)
    (output/('releases.'+channel+'.json')).write_text(json.dumps(feed,separators=(',',':')),encoding='utf-8')
    if sys.platform == 'win32':
        normalize_setup(output, channel)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--name', default='SHAQ Daily Oracle Lab')
    parser.add_argument('--version', help='Explicit artifact-only version override; recorded in provenance')
    parser.add_argument('--manage-existing', type=Path, help='Pack an already built full native payload with pinned Velopack')
    parser.add_argument('--prepare-public-base', action='store_true', help='Final release only: seed fresh output with a verified public delta base')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.manage_existing:
        version = args.version or tomllib.loads((root/'pyproject.toml').read_text())['project']['version']
        if args.prepare_public_base:
            from release_feed import prepare_public_base
            receipt = prepare_public_base(root, args.output.resolve(), version, platform.system(), platform.machine())
            print(json.dumps(receipt), flush=True)
        pack_managed(root, args.manage_existing.resolve(), args.output.resolve(), version)
        return
    if args.prepare_public_base:
        parser.error('--prepare-public-base requires --manage-existing')
    if sys.version_info[:2] != (3, 13):
        raise RuntimeError('Native release builds require CPython 3.13')
    import tables
    if tables.which_lib_version('lzo') is not None:
        raise RuntimeError('Use the no-LZO source build before packaging')
    blosc2_library = (stage_macos_blosc2(Path(tables.__file__).parent, root / 'build/native-loader')
                      if sys.platform == 'darwin' else None)
    project_metadata = stage_version(root, args.version) if args.version else None
    collect_notices(root, args.version)
    env = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(root / 'build/pyinstaller-cache'))
    subprocess.run([sys.executable, '-m', 'PyInstaller', *pyinstaller_args(root, args.output.resolve(), args.name, blosc2_library, project_metadata)], check=True, env=env)
    if sys.platform == 'darwin':
        set_macos_bundle_identity(root, args.output / (args.name + '.app'), args.version)
    # pip's local installation URL is not runtime metadata. Public provenance is
    # retained in third-party/source-manifest.json and the wheel/source artifacts.
    for payload in (args.output / args.name, args.output / (args.name + '.app')):
        for path in payload.rglob('*.dist-info/direct_url.json'):
            path.unlink()


if __name__ == '__main__':
    main()
