"""Restore only verified native dependencies for a GUI diagnostic, never an app."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys


def verify(application, artifact_root, run, artifact, run_id, artifact_id):
    repository = json.loads((application/'config/team-repository.json').read_text())
    official = repository['owner']+'/'+repository['repository']
    sha = subprocess.check_output(['git','rev-parse','HEAD'],cwd=application,text=True).strip()
    origin = artifact.get('workflow_run', {})
    repo = run.get('repository', {})
    if (run.get('id') != run_id or run.get('path') != '.github/workflows/build-desktop.yml' or
            repo.get('full_name') != official or not repo.get('id') or
            run.get('head_sha') != sha or artifact.get('id') != artifact_id or artifact.get('expired') is not False or
            artifact.get('name') != 'SHAQ-Daily-Oracle-Lab-Windows-x64' or origin.get('id') != run_id or
            origin.get('head_sha') != sha or origin.get('repository_id') != repo['id'] or
            origin.get('head_repository_id') != repo['id']):
        raise ValueError('Dependency artifact must belong to the official native workflow and exact application commit')
    return verify_wheels(application, artifact_root)


def verify_wheels(application, artifact_root):
    """Common wheel integrity checks; source identity is checked by each caller."""
    directory = artifact_root/'build/native-dependencies'
    hashes = json.loads((directory/'wheel-sha256.json').read_text())
    pins = {name.lower().replace('-','_'): version for name,version in re.findall(
        r'^([\w-]+)==([^\s;]+)', (application/'packaging/requirements.lock.txt').read_text(), re.M)}
    required = {'tables','quickjs','bcolz_zipline'}
    if len(hashes) != 3 or {name.split('-')[0].lower() for name in hashes} != required:
        raise ValueError('Expected exactly the three native diagnostic dependency wheels')
    wheels = []
    if {path.name for path in (directory/'wheels').iterdir()} != set(hashes):
        raise ValueError('Unexpected or missing native wheel files')
    for name, digest in hashes.items():
        parts = name.split('-')
        if (Path(name).name != name or len(parts) != 5 or parts[1] != pins.get(parts[0].lower()) or
                not name.endswith('-win_amd64.whl') or not re.fullmatch('[a-fA-F0-9]{64}', str(digest))):
            raise ValueError('Native wheel identity differs from the locked Windows dependencies')
        wheel = directory/'wheels'/name
        if wheel.is_symlink() or hashlib.sha256(wheel.read_bytes()).hexdigest() != digest.lower():
            raise ValueError('Native diagnostic wheel SHA256 mismatch')
        wheels.append(wheel)
    return sorted(wheels)


def install_dependencies(application, wheels):
    pip = [sys.executable,'-m','pip']
    subprocess.run([*pip,'install','--no-deps',*[str(p) for p in wheels]],check=True)
    # Preserve the three verified native wheels. Other exact locked pins (for
    # example peewee/proxy_tools) may need ordinary sdist installation wheels.
    native_names = ','.join(sorted(p.name.split('-')[0].replace('_','-') for p in wheels))
    subprocess.run([*pip,'install','--only-binary='+native_names,'-r',str(application/'packaging/requirements.lock.txt')],check=True)
    subprocess.run([*pip,'install','--no-deps','--no-build-isolation','-e',str(application)],check=True)
    subprocess.run([*pip,'check'],check=True)
    subprocess.run([sys.executable,'-c',"import tables; assert tables.which_lib_version('lzo') is None"],check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--application', type=Path, required=True)
    parser.add_argument('--artifact-root', type=Path, required=True)
    parser.add_argument('--run-metadata', type=Path, required=True)
    parser.add_argument('--artifact-metadata', type=Path, required=True)
    parser.add_argument('--run-id', type=int, required=True)
    parser.add_argument('--artifact-id', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = {'status':'failed','validation_sha':os.environ.get('GITHUB_SHA'),
              'run_id':args.run_id,'artifact_id':args.artifact_id,'purpose':'dependency-only GUI diagnostic'}
    try:
        if sys.platform != 'win32' or sys.version_info[:2] != (3,13) or platform.machine().lower() not in ('amd64','x86_64'):
            raise ValueError('Diagnostic dependency restore requires native Windows x64 CPython 3.13')
        run = json.loads(args.run_metadata.read_text(encoding='utf-8-sig'))
        artifact = json.loads(args.artifact_metadata.read_text(encoding='utf-8-sig'))
        wheels = verify(args.application,args.artifact_root,run,artifact,args.run_id,args.artifact_id)
        result.update(application_sha=run['head_sha'],wheel_sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in wheels})
        install_dependencies(args.application, wheels)
        result['status']='passed'
    except Exception as exc:
        result['error']=str(exc)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    return 0 if result['status']=='passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
