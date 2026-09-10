"""Audit the actual complete native payload, not just the PyTables directory."""
from pathlib import Path
import argparse
import hashlib
import json
import platform
import re
import subprocess
import sys


def forbidden_paths(root):
    found = []
    for path in sorted(root.rglob('*')):
        parts = {p.lower() for p in path.relative_to(root).parts}
        name = path.name.lower()
        if parts & {'backtrader', 'runtime', '.git', '.env'} or 'liblzo' in name or name.startswith('lzo2.'):
            found.append(str(path.relative_to(root)))
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
        metadata = json.loads(manifest.read_text())
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


def audit(root):
    failures = forbidden_paths(root) + verify_methods(root)
    native = []
    upstream_paths = []
    private_prefixes = (str(Path.home()), str(Path(__file__).resolve().parents[1]))
    for path in root.rglob('*'):
        if not path.is_file() or path.is_symlink():
            continue
        with path.open('rb') as stream:
            magic = stream.read(4)
        data = path.read_bytes()
        if any(prefix.encode() in data for prefix in private_prefixes):
            # User names in debug/source paths are private, even if linking is relocatable.
            failures.append(f'private macOS user path: {path.relative_to(root)}')
        elif b'/Users/' in data:
            upstream_paths.append(str(path.relative_to(root)))
        if sys.platform == 'darwin' and magic in (b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xfe\xed\xfa\xcf'):
            output = subprocess.check_output(['otool', '-L', str(path)], text=True)
            links = [line.strip().split(' (')[0] for line in output.splitlines()[1:] if line.startswith('\t')]
            architecture = subprocess.check_output(['lipo', '-archs', str(path)], text=True).strip()
            if platform.machine() not in architecture.split():
                failures.append(f'wrong architecture: {path.relative_to(root)}: {architecture}')
            load_commands = subprocess.check_output(['otool', '-l', str(path)], text=True)
            minimum = re.findall(r'\bminos\s+(\S+)|LC_VERSION_MIN_MACOSX\s+cmdsize\s+\d+\s+version\s+(\S+)', load_commands)
            failures.extend(f'{path.relative_to(root)} -> {link}' for link in bad_macos_links(links))
            native.append({'path': str(path.relative_to(root)), 'links': links, 'architecture': architecture,
                           'minimum_versions': [a or b for a, b in minimum]})
        elif sys.platform == 'win32' and magic[:2] == b'MZ':
            import pefile
            pe = pefile.PE(str(path))
            links = [entry.dll.decode() for entry in getattr(pe, 'DIRECTORY_ENTRY_IMPORT', [])]
            failures.extend(f'{path.relative_to(root)} -> {link}' for link in links if 'lzo' in link.lower())
            native.append({'path': str(path.relative_to(root)), 'links': links})
            pe.close()
        if path.suffix.lower() in {'.json', '.toml', '.yaml', '.yml', '.ini', '.env'}:
            content = path.read_text(errors='replace')
            if re.search(r'sk-proj-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}', content):
                failures.append(f'private material: {path.relative_to(root)}')
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
