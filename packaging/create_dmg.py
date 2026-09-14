"""Bounded native DMG creation; failed attempts never replace an accepted image."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile
import time


DEFAULT_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 5
DEFAULT_TIMEOUT_SECONDS = 300
MAX_ATTEMPTS = 10
MAX_BACKOFF_SECONDS = 60
BUSY_DIAGNOSTIC = 'hdiutil: create failed - Resource busy'


def create_dmg(source, output, volume, *, attempts=DEFAULT_ATTEMPTS,
               backoff_seconds=DEFAULT_BACKOFF_SECONDS, timeout_seconds=DEFAULT_TIMEOUT_SECONDS):
    if not 1 <= attempts <= MAX_ATTEMPTS or not 0 <= backoff_seconds <= MAX_BACKOFF_SECONDS or timeout_seconds <= 0:
        raise ValueError('Invalid bounded DMG retry configuration')
    source = Path(source).resolve(strict=True)
    output = Path(output).absolute()
    parent = output.parent.resolve(strict=True)
    if not source.is_dir() or output.is_symlink() or (output.exists() and not output.is_file()):
        raise ValueError('DMG source/output must be a directory and a regular image path')
    output = parent / output.name
    attempts_root = Path(tempfile.mkdtemp(prefix=output.name + '.create-', dir=parent))
    print(f'DMG attempt diagnostics: {attempts_root}', flush=True)
    for number in range(1, attempts + 1):
        partial = attempts_root / f'attempt-{number}.dmg'
        command = ['hdiutil', 'create', '-volname', volume, '-srcfolder', str(source),
                   '-format', 'UDZO', str(partial)]
        log = attempts_root / f'attempt-{number}.log'
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding='utf-8', errors='replace', timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            diagnostic = exc.stdout or b''
            if isinstance(diagnostic, bytes):
                diagnostic = diagnostic.decode('utf-8', errors='replace')
            log.write_text(diagnostic + '\n' + str(exc), encoding='utf-8')
            print(diagnostic, end='', flush=True)
            raise
        log.write_text(result.stdout, encoding='utf-8')
        print(result.stdout, end='', flush=True)
        if result.returncode == 0:
            if not partial.is_file() or partial.is_symlink():
                raise RuntimeError('hdiutil reported success without a regular image')
            partial.replace(output)
            return 0
        if partial.is_file() and not partial.is_symlink():
            partial.unlink()  # Only our new, failed attempt; never the final output.
        # create returns 1 for busy (detach uses 16); status 1 alone is not retryable.
        # https://github.com/create-dmg/create-dmg/blob/master/create-dmg (hdiutil_retry)
        busy = result.returncode == 1 and BUSY_DIAGNOSTIC in result.stdout.splitlines()
        if not busy or number == attempts:
            return result.returncode
        time.sleep(backoff_seconds)
    raise AssertionError('Unreachable bounded DMG loop')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--volume', required=True)
    args = parser.parse_args()
    return create_dmg(args.source, args.output, args.volume,
                      attempts=int(os.environ.get('SHAQ_DMG_ATTEMPTS', DEFAULT_ATTEMPTS)),
                      backoff_seconds=float(os.environ.get('SHAQ_DMG_BACKOFF_SECONDS', DEFAULT_BACKOFF_SECONDS)),
                      timeout_seconds=float(os.environ.get('SHAQ_DMG_TIMEOUT_SECONDS', DEFAULT_TIMEOUT_SECONDS)))


if __name__ == '__main__':
    raise SystemExit(main())
