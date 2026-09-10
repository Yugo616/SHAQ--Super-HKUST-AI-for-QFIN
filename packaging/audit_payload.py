"""Audit the actual complete native payload, not just the PyTables directory."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys


def forbidden_paths(root):
    found = []
    for path in sorted(root.rglob('*')):
        relative = tuple(p.lower() for p in path.relative_to(root).parts)
        parts = set(relative)
        # SHAQ state is rooted at the payload, not in dependency namespaces such
        # as pythonnet/runtime. Include PyInstaller's platform payload containers.
        payload = relative
        for container in (('_internal',), ('contents', 'resources'), ('contents', 'frameworks')):
            if relative[:len(container)] == container:
                payload = relative[len(container):]
                break
        private_runtime = payload[:1] == ('runtime',) or payload[:2] == ('shaq_daily_oracle', 'runtime')
        name = path.name.lower()
        if private_runtime or parts & {'backtrader', '.git', '.env'} or 'liblzo' in name or name.startswith('lzo2.'):
            found.append(path.relative_to(root).as_posix())
    return found


def bad_macos_links(links):
    return [link for link in links if 'liblzo' in link.lower() or (
        link.startswith('/') and not link.startswith(('/usr/lib/', '/System/Library/')))]


def verify_methods(root):
    manifests = list(root.rglob('third-party/manifest.json'))
    if not manifests:
        return ['missing bundled provenance manifest']
    failures = []
    for manifest in manifests:
        package = manifest.parent.parent
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
        methods = metadata['methods']
        for name, digest in methods.items():
            path = package / 'bundled_versions' / name
            if not path.is_file():
                failures.append(f'bundled method missing: {name}')
            elif hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                failures.append(f'bundled method hash mismatch: {name}')
        for distribution in metadata.get('distributions', []):
            for name in distribution['notices']:
                path = manifest.parent / name
                if not path.is_file() or not path.stat().st_size:
                    failures.append(f'bundled license missing: {name}')
    return failures


def contains_private_path(data, home, checkout, hosted_runner=False, verified_upstream=False):
    # CMake and native compilers use both Windows slash spellings.
    data = data.replace(b'\\', b'/')
    home, checkout = home.replace('\\', '/'), checkout.replace('\\', '/')
    if re.match(r'^[A-Za-z]:/', home):
        data, home, checkout = data.lower(), home.lower(), checkout.lower()
    if checkout.encode() in data:
        return True
    home_bytes = home.encode()
    for match in re.finditer(re.escape(home_bytes), data):
        # Upstream wheels retain generic hosted build and Cargo source locations.
        # Only this exact hosted identity gets the exception, never a personal or
        # self-hosted home, current checkout, settings, credentials, or other data.
        suffix = data[match.end():]
        upstream = (hosted_runner and home.split('/') == ['', 'Users', 'runner'] and
                    (re.match(rb'/work/([^/\x00\r\n]+)/\1/', suffix) or
                     suffix.startswith(b'/.cargo/registry/src/')))
        if hosted_runner and home == 'c:/users/runneradmin':
            # Verified locked-wheel source locations, not arbitrary Temp contents.
            cargo = re.match(rb'/\.cargo/registry/src/index\.crates\.io-[a-f0-9]+/', suffix)
            source = re.match(
                rb'/appdata/local/temp/tmp[a-z0-9_]+/'
                rb'(?:hdf5-hdf5_[0-9][0-9._-]*/src/|build/_deps/blosc2-src/'
                rb'(?:blosc/|include/|plugins/(?:codecs|filters)/))'
                rb'(?:[a-z0-9_-]+/)*[a-z0-9_.-]+\.[ch](?=[\x00\r\n]|$)', suffix)
            settings = (b'summary of the hdf5 configuration' in data and
                        data[:match.start()].endswith(b'module directory: ') and
                        re.match(rb'/appdata/local/temp/tmp[a-z0-9_]+/mod\r?\n', suffix))
            proven_build_source = (verified_upstream and suffix.startswith(b'/appdata/local/temp/') and
                                   b'/../' not in suffix.split(b'\x00', 1)[0])
            upstream = source or settings or proven_build_source or (cargo and b'/../' not in suffix.split(b'\x00', 1)[0])
        if not upstream:
            return True
    return False


def audit(root):
    failures = forbidden_paths(root) + verify_methods(root)
    native = []
    upstream_paths = []
    proven_files = {}
    for manifest in root.rglob('third-party/upstream-path-provenance.json'):
        for proof in json.loads(manifest.read_text(encoding='utf-8')):
            for name, digest in proof['files'].items():
                proven_files.setdefault(Path(name).name, set()).add(digest)
    home, checkout = str(Path.home()), str(Path(__file__).resolve().parents[1])
    hosted_runner = os.environ.get('GITHUB_ACTIONS') == 'true' and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted'
    for path in root.rglob('*'):
        if not path.is_file() or path.is_symlink():
            continue
        with path.open('rb') as stream:
            magic = stream.read(4)
        data = path.read_bytes()
        verified_upstream = hashlib.sha256(data).hexdigest() in proven_files.get(path.name, set())
        if contains_private_path(data, home, checkout, hosted_runner, verified_upstream):
            # User names in debug/source paths are private, even if linking is relocatable.
            failures.append(f'private build/user path: {path.relative_to(root).as_posix()}')
        elif verified_upstream or b'/Users/' in data or b'C:\\Users\\runneradmin' in data:
            upstream_paths.append(path.relative_to(root).as_posix())
        if sys.platform == 'darwin' and magic in (b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xfe\xed\xfa\xcf'):
            output = subprocess.check_output(['otool', '-L', str(path)], text=True)
            links = [line.strip().split(' (')[0] for line in output.splitlines()[1:] if line.startswith('\t')]
            architecture = subprocess.check_output(['lipo', '-archs', str(path)], text=True).strip()
            if platform.machine() not in architecture.split():
                failures.append(f'wrong architecture: {path.relative_to(root).as_posix()}: {architecture}')
            load_commands = subprocess.check_output(['otool', '-l', str(path)], text=True)
            minimum = re.findall(r'\bminos\s+(\S+)|LC_VERSION_MIN_MACOSX\s+cmdsize\s+\d+\s+version\s+(\S+)', load_commands)
            failures.extend(f'{path.relative_to(root).as_posix()} -> {link}' for link in bad_macos_links(links))
            native.append({'path': path.relative_to(root).as_posix(), 'links': links, 'architecture': architecture,
                           'minimum_versions': [a or b for a, b in minimum]})
        elif sys.platform == 'win32' and magic[:2] == b'MZ':
            import pefile
            pe = pefile.PE(str(path))
            links = [entry.dll.decode() for entry in getattr(pe, 'DIRECTORY_ENTRY_IMPORT', [])]
            failures.extend(f'{path.relative_to(root).as_posix()} -> {link}' for link in links if 'lzo' in link.lower())
            native.append({'path': path.relative_to(root).as_posix(), 'links': links})
            pe.close()
        if path.suffix.lower() in {'.json', '.toml', '.yaml', '.yml', '.ini', '.env'}:
            content = path.read_text(encoding='utf-8', errors='replace')
            if re.search(r'sk-proj-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}', content):
                failures.append(f'private material: {path.relative_to(root).as_posix()}')
    if not native:
        failures.append('no native payload inspected')
    return {'status': 'failed' if failures else 'passed', 'native_count': len(native), 'failures': failures,
            'upstream_user_path_files': upstream_paths, 'native': native}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.root)
    args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in result.items() if key != 'native'}))
    return 0 if result['status'] == 'passed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
