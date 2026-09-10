"""Wait for Inno self-cleanup and report leftovers without deleting anything."""
import argparse
import json
from pathlib import Path
import time


def wait_for_uninstall(root, timeout=30, poll_interval=0.25):
    deadline = time.monotonic() + timeout
    while root.exists() or root.is_symlink():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval, remaining))
    exists = root.exists() or root.is_symlink()
    leftovers = ['.'] + sorted(str(path.relative_to(root)) for path in root.rglob('*')) if exists else []
    return {'status': 'failed' if exists else 'passed', 'install_root': str(root),
            'timeout_seconds': timeout, 'leftovers': leftovers}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout-seconds', type=float, default=30)
    args = parser.parse_args()
    if not 0 <= args.timeout_seconds <= 60:
        parser.error('timeout must be between 0 and 60 seconds')
    result = wait_for_uninstall(args.root, timeout=args.timeout_seconds)
    args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))
    return 0 if result['status'] == 'passed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
