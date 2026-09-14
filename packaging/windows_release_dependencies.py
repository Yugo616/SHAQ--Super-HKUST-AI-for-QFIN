"""Reuse official native wheels with full frozen-source distribution provenance."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile

from windows_layout_dependencies import verify_wheels, install_dependencies


def digest(path):
    if path.is_symlink(): raise ValueError('Dependency provenance cannot use symlinks')
    with path.open('rb') as stream: return hashlib.file_digest(stream,'sha256').hexdigest()


def verify_release(application, artifact_root, run, artifact, run_id, artifact_id):
    def read(relative):
        path=artifact_root/relative
        if path.is_symlink(): raise ValueError('Dependency provenance cannot use symlinks')
        return json.loads(path.read_text(encoding='utf-8-sig'))
    sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=application,text=True).strip()
    if subprocess.check_output(['git','status','--porcelain'],cwd=application,text=True).strip():
        raise ValueError('Dependency reuse requires clean application source')
    repository=json.loads((application/'config/team-repository.json').read_text())
    official=repository['owner']+'/'+repository['repository']
    source=read('dist/diagnostic/external-layout-source.json')
    origin=artifact.get('workflow_run',{});repo=run.get('repository',{})
    validation=run.get('head_sha','')
    if (not re.fullmatch('[a-f0-9]{40}',sha) or not re.fullmatch('[a-f0-9]{40}',validation) or
            source.get('application_sha') != sha or source.get('validation_sha') != validation or
            any(not re.fullmatch('[a-fA-F0-9]{64}',source.get(field,'')) for field in
                ('layout_sha256','geometry_sha256','display_preflight_sha256')) or
            run.get('id') != run_id or run.get('path') != '.github/workflows/build-desktop.yml' or
            repo.get('full_name') != official or not repo.get('id') or artifact.get('id') != artifact_id or
            artifact.get('expired') is not False or artifact.get('name') != 'SHAQ-Daily-Oracle-Lab-Windows-x64' or
            origin.get('id') != run_id or origin.get('head_sha') != validation or
            origin.get('repository_id') != repo['id'] or origin.get('head_repository_id') != repo['id']):
        raise ValueError('Official workflow/artifact/application/validation provenance mismatch')
    manifest=read('build/third-party/manifest.json')
    methods={path.relative_to(application/'bundled_versions').as_posix():digest(path)
             for path in (application/'bundled_versions').rglob('*') if path.is_file()}
    recorded_methods={name.replace('\\','/'):value for name,value in manifest.get('methods',{}).items()}
    if (manifest.get('source_sha') != sha or manifest.get('source_dirty') is not False or
            manifest.get('architecture') != 'AMD64' or not manifest.get('python','').startswith('3.13.') or
            recorded_methods != methods):
        raise ValueError('Pristine application build manifest does not match frozen source')
    wheels=verify_wheels(application,artifact_root)
    distributions={row['name'].lower().replace('-','_'):row['version'] for row in manifest.get('distributions',[])}
    if any(distributions.get(wheel.name.split('-')[0]) != wheel.name.split('-')[1] for wheel in wheels):
        raise ValueError('Native wheel versions differ from completed build manifest')
    native=artifact_root/'build/native-dependencies'
    spec=json.loads((application/'packaging/native-sources.json').read_text())
    recorded=read('build/third-party/source-manifest.json')
    archives=[]
    for name,item in spec.items():
        archive=native/'sources'/item['url'].rsplit('/',1)[-1]
        if recorded.get(name) != item or digest(archive) != item['sha256']:
            raise ValueError('Locked native source archive/provenance mismatch')
        archives.append(archive)
    mapping=read('build/native-dependencies/hdf5-diagnostic-map.json')
    hdf=native/'sources'/spec['hdf5']['url'].rsplit('/',1)[-1]
    with tarfile.open(hdf) as archive:
        mapped={str(Path(*Path(member.name).parts[1:])).replace('\\','/') for member in archive.getmembers()
                if member.isfile() and Path(member.name).suffix in ('.c','.h')}
    if (mapping != read('build/third-party/hdf5/diagnostic-map.json') or
            mapping.get('source_sha256') != spec['hdf5']['sha256'] or
            mapping.get('recipe') != 'packaging/build_native.py:map_hdf_source_locations' or
            mapping.get('transformation') != 'Prepend #line 1 with source-relative filename to extracted .c/.h build copies; original following bytes unchanged.' or
            set(mapping.get('files',[])) != mapped):
        raise ValueError('HDF5 mapping provenance differs from verified source')
    mingw=read('build/native-dependencies/mingw-toolchain.json')
    if (mingw != read('build/third-party/MinGW-W64/toolchain.json') or not mingw.get('packages') or
            not re.fullmatch('[a-f0-9]{64}',mingw.get('compiler_sha256',''))):
        raise ValueError('MinGW compiled-wheel provenance is missing or inconsistent')
    return {'application_sha':sha,'source_validation_sha':validation,'wheels':wheels,'archives':archives,
            'mingw':mingw,'metadata':[native/'wheel-sha256.json',native/'mingw-toolchain.json',native/'hdf5-diagnostic-map.json']}


def restore(application, verified):
    # Keep the original builder's compiler/runtime notice check. No native build.
    spec=importlib.util.spec_from_file_location('frozen_build_desktop',application/'packaging/build_desktop.py')
    builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)
    if builder.mingw_toolchain()[2] != verified['mingw']:
        raise ValueError('Current MSYS2 compiler/runtime differs from reused native wheel provenance')
    destination=application/'build/native-dependencies'
    for source in [*verified['metadata'],*verified['wheels'],*verified['archives']]:
        subdir='wheels' if source.suffix=='.whl' else ('sources' if source.name.endswith('.tar.gz') else '')
        target=destination/subdir/source.name
        target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists() and digest(target) != digest(source):
            raise ValueError('Refusing to overwrite different existing native dependency evidence')
        shutil.copy2(source,target)
    install_dependencies(application,verified['wheels'])


def main():
    parser=argparse.ArgumentParser()
    for name in ('application','artifact-root','run-metadata','artifact-metadata','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--run-id',type=int,required=True);parser.add_argument('--artifact-id',type=int,required=True)
    args=parser.parse_args();result={'status':'failed','purpose':'dependency-only release reuse','validation_sha':os.environ.get('GITHUB_SHA')}
    try:
        if sys.platform != 'win32' or sys.version_info[:2] != (3,13) or platform.machine().lower() not in ('amd64','x86_64'):
            raise ValueError('Release dependency reuse requires native Windows x64 CPython3.13')
        run=json.loads(args.run_metadata.read_text(encoding='utf-8-sig'))
        artifact=json.loads(args.artifact_metadata.read_text(encoding='utf-8-sig'))
        verified=verify_release(args.application,args.artifact_root,run,artifact,args.run_id,args.artifact_id)
        result.update(application_sha=verified['application_sha'],source_validation_sha=verified['source_validation_sha'],
                      source_run_id=args.run_id,source_artifact_id=args.artifact_id,
                      files_sha256={str(path.relative_to(args.artifact_root)):digest(path) for path in
                          [*verified['wheels'],*verified['archives'],*verified['metadata']]})
        restore(args.application,verified)
        result['status']='passed'
    except Exception as exc: result['error']=str(exc)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result),flush=True)
    return 0 if result['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
