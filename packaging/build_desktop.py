"""One payload recipe for the release and preview, on the actual host architecture."""
from pathlib import Path
import argparse
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


def pyinstaller_args(root, output, name, blosc2_library=None):
    args = ['--noconfirm', '--clean', '--windowed', '--onedir', '--name', name,
            '--paths', str(root / 'src'), '--distpath', str(output),
            '--runtime-hook', str(root / 'packaging/frozen_native.py'),
            '--workpath', str(root / 'build/desktop-native'), '--specpath', str(root / 'build')]
    if sys.platform == 'darwin':
        args += ['--strip']
    if blosc2_library is not None:
        args += ['--add-binary', f'{blosc2_library}:tables']
    args += ['--collect-submodules', 'scipy._external']
    for package in ('webview', 'zipline', 'tables', 'bcolz', 'pandas_market_calendars',
                    'exchange_calendars', 'yfinance', 'keyring'):
        args += ['--collect-all', package]
    for package in ('openai', 'quickjs', '_quickjs', 'keyring.backends.macOS' if sys.platform == 'darwin' else 'keyring.backends.Windows'):
        args += ['--hidden-import', package]
    for package in ('rfc3987_syntax', 'iso4217', 'shaq_daily_oracle'):
        args += ['--collect-data', package]
    for resource in ('config', 'governance', 'schemas', 'skills', 'decision', 'bundled_versions'):
        args += ['--add-data', f'{root / resource}:{resource}']
    args += ['--add-data', f'{root / "pyproject.toml"}:.',
             '--add-data', f'{root / "src/shaq_daily_oracle/desktop"}:shaq_daily_oracle/desktop',
             '--add-data', f'{root / "build/third-party"}:third-party',
             str(root / 'packaging/desktop_entry.py')]
    return args


def collect_notices(root):
    destination = root / 'build/third-party'
    if destination.is_dir():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
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
        compiler = shutil.which('gcc')
        licenses = Path(compiler).parent.parent / 'share/licenses' if compiler else None
        if licenses is None or not licenses.is_dir():
            raise RuntimeError('MinGW-W64 runtime licenses are required for the native Windows build')
        shutil.copytree(licenses, destination / 'MinGW-W64', dirs_exist_ok=True)
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
                'methods': methods, 'distributions': distributions}
    (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--name', default='SHAQ Daily Oracle Lab')
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 13):
        raise RuntimeError('Native release builds require CPython 3.13')
    import tables
    if tables.which_lib_version('lzo') is not None:
        raise RuntimeError('Use the no-LZO source build before packaging')
    root = Path(__file__).resolve().parents[1]
    blosc2_library = (stage_macos_blosc2(Path(tables.__file__).parent, root / 'build/native-loader')
                      if sys.platform == 'darwin' else None)
    collect_notices(root)
    env = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(root / 'build/pyinstaller-cache'))
    subprocess.run([sys.executable, '-m', 'PyInstaller', *pyinstaller_args(root, args.output.resolve(), args.name, blosc2_library)], check=True, env=env)
    # pip's local installation URL is not runtime metadata. Public provenance is
    # retained in third-party/source-manifest.json and the wheel/source artifacts.
    for payload in (args.output / args.name, args.output / (args.name + '.app')):
        for path in payload.rglob('*.dist-info/direct_url.json'):
            path.unlink()


if __name__ == '__main__':
    main()
