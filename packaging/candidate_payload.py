"""Reuse one clean same-source native candidate without another compilation."""
import argparse
import json
from pathlib import Path
import platform
import subprocess


def verify_candidate(root, payload, version):
    payload = Path(payload).resolve()
    manifests = {path.resolve() for path in payload.rglob('third-party/manifest.json')}
    if len(manifests) != 1:
        raise ValueError('Candidate requires one unambiguous bundled provenance manifest')
    metadata = json.loads(manifests.pop().read_text(encoding='utf-8'))
    current = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    dirty = subprocess.check_output(['git', 'status', '--porcelain'], cwd=root, text=True).strip()
    if (dirty or metadata.get('source_sha') != current or metadata.get('source_dirty') is not False or
            metadata.get('version_override') != version or metadata.get('source_version') != version or
            metadata.get('architecture') != platform.machine()):
        raise ValueError('Candidate source, cleanliness, version or architecture differs; rebuild required')
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('payload', type=Path)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    print(verify_candidate(Path(__file__).resolve().parents[1], args.payload, args.version))


if __name__ == '__main__':
    main()
