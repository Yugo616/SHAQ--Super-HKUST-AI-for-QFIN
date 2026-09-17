"""Manual release operator: accepted artifacts only, draft publication only."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

root=Path.cwd()
repo=os.environ['GITHUB_REPOSITORY']
sha=os.environ['SHAQ_APPLICATION_SHA']
version=os.environ['SHAQ_RELEASE_VERSION']
if not re.fullmatch(r'[a-f0-9]{40}',sha) or not re.fullmatch(r'\d+\.\d+\.\d+',version):
    raise ValueError('Invalid release identity')
os.environ['SHAQ_APPLICATION_ROOT']=str(root/'application')
from stage_accepted_release import stage,digest

def gh(*args):
    return subprocess.check_output(['gh',*map(str,args)],text=True)

def api(path):return json.loads(gh('api',f'repos/{repo}/{path}'))

targets=[('Windows-x64','WINDOWS_RUN','windows'),
         ('macOS-Apple-Silicon','ARM_RUN','macos'),('macOS-Intel','INTEL_RUN','macos')]
plans=[]
for platform,key,suffix in targets:
    run_id=os.environ[key]
    if not run_id.isdigit():raise ValueError('Invalid native run ID')
    run=api('actions/runs/'+run_id)
    if run['head_sha']!=sha or run['repository']['full_name']!=repo:
        raise ValueError('Native source/repository mismatch')
    jobs=api('actions/runs/'+run_id+'/jobs')
    selected=[j for j in jobs['jobs'] if platform in j['name'] and j['conclusion']=='success']
    if len(selected)!=1:raise ValueError('Platform acceptance has not passed: '+platform)
    plans.append((platform,run_id,suffix,jobs))

for suffix in ('windows','macos'):
    result=subprocess.run(['gh','release','view',f'lab-v{version}-{suffix}','--repo',repo],
                          capture_output=True,text=True)
    if result.returncode==0:raise ValueError('Release already exists')
    if 'release not found' not in (result.stderr+result.stdout).lower():
        raise ValueError('Could not verify release absence')

for platform,run_id,suffix,jobs in plans:
    artifact=root/'artifacts'/platform
    name='SHAQ-Daily-Oracle-Lab-'+platform
    gh('run','download',run_id,'--repo',repo,'--name',name,'--dir',artifact)
    stage(artifact,root/'staged'/platform,platform,jobs)

for suffix in ('windows','macos'):
    destination=root/'publish'/suffix;destination.mkdir(parents=True)
    for platform,_,item_suffix,_ in plans:
        if suffix!=item_suffix:continue
        for path in (root/'staged'/platform).iterdir():
            if path.name=='SHA256SUMS.txt':continue
            target=destination/path.name
            if target.exists():raise ValueError('Conflicting release asset')
            shutil.copy2(path,target)
    sums=''.join(f'{digest(p)}  {p.name}\n' for p in sorted(destination.iterdir()))
    (destination/'SHA256SUMS.txt').write_text(sums)
    tag=f'lab-v{version}-{suffix}'
    title=f'SHAQ Lab {version} · '+('Windows' if suffix=='windows' else 'macOS')
    gh('release','create',tag,'--repo',repo,'--target',sha,'--draft','--prerelease',
       '--title',title,'--notes-file',root/'packaging/accepted-release-notes.md')
    gh('release','upload',tag,'--repo',repo,*sorted(destination.iterdir()))
    release=api('releases/tags/'+tag)
    remote={x['name']:x for x in release['assets']}
    for path in destination.iterdir():
        asset=remote.get(path.name,{})
        if asset.get('size')!=path.stat().st_size or asset.get('digest')!='sha256:'+digest(path):
            raise ValueError('Uploaded release asset mismatch: '+path.name)
    print(json.dumps({'tag':tag,'draft':True,'application_sha':sha,'verified_assets':len(remote)}),flush=True)
