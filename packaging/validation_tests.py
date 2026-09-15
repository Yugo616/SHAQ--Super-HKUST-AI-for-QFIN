"""Run reviewed tests against byte-identical frozen application runtime files."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def runtime_files(root):
    paths=[]
    for name in ('src','skills','bundled_versions','config','schemas'):
        paths.extend(path for path in (root/name).rglob('*') if path.is_file()
                     and not any(part=='__pycache__' or part.endswith('.egg-info') for part in path.parts)
                     and path.suffix not in ('.pyc','.pyo'))
    paths.extend(root/name for name in ('pyproject.toml','packaging/requirements.lock.txt'))
    return {path.relative_to(root).as_posix():hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths}


def verify_runtime(application, validation):
    expected, actual=runtime_files(application), runtime_files(validation)
    if expected != actual:
        raise ValueError('External tests cannot change the frozen application runtime')
    return expected


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--application',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    validation=Path(__file__).resolve().parents[1]
    files=verify_runtime(args.application.resolve(),validation)
    # Import the verified byte-identical copy so path-sensitive fixture checks
    # stay rooted in their isolated validation tree, never user storage.
    environment=dict(os.environ, PYTHONPATH=os.pathsep.join((str(validation/'src'),str(validation/'tests'))))
    result=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-v'],
                          cwd=validation,env=environment)
    receipt={'status':'passed' if result.returncode==0 else 'failed',
             'application_sha':os.environ.get('SHAQ_LAYOUT_APPLICATION_SHA'),
             'validation_sha':os.environ.get('SHAQ_LAYOUT_VALIDATION_SHA'),
             'runtime_files_sha256':files,'exit_code':result.returncode}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(receipt,indent=2),encoding='utf-8')
    return result.returncode


if __name__=='__main__':raise SystemExit(main())
