"""Isolated native install/update/replay/uninstall using the common build recipe."""
import argparse
import functools
import hashlib
import http.server
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile


class FaultHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        mode, _, remaining=self.path.lstrip('/').partition('/')
        if mode.startswith('fault-'):
            self.path='/'+remaining
            target=self.path.split('?',1)[0]
            corrupt=(mode=='fault-delta' and target.endswith('-delta.nupkg')) or (mode in ('fault-full','fault-network') and target.endswith('-full.nupkg'))
            if corrupt:
                self.send_response(200)
                self.send_header('Content-Length',str(100000 if mode=='fault-network' else 3))
                self.end_headers();self.wfile.write(b'bad');self.wfile.flush();self.close_connection=True
                return
        super().do_GET()


def native_download_faults(root,feed,installed,configuration):
    """Real SDK/download/native patch, outside the full-app GUI acceptance."""
    import velopack
    from shaq_daily_oracle.update_native import verify_cached
    config=json.loads(configuration.read_text())
    base=next(feed.glob('*-'+config['bridge_version']+'-*-full.nupkg'))
    manifest=root/'fault-base.nuspec'
    with zipfile.ZipFile(base) as archive:
        names=[name for name in archive.namelist() if name.endswith('.nuspec')]
        if len(names)!=1:raise RuntimeError('Ambiguous base package manifest')
        manifest.write_bytes(archive.read(names[0]))
    binary=installed/'Contents/MacOS' if sys.platform=='darwin' else installed/'current'
    original=binary/'UpdateMac' if sys.platform=='darwin' else installed/'Update.exe'
    updater=root/original.name;shutil.copy2(original,updater)
    results={}
    for mode in ('no-base','delta','full','network','cached-corrupt'):
        cache=root/('fault-cache-'+mode);cache.mkdir()
        if mode=='delta':shutil.copy2(base,cache/base.name)
        locator=velopack.VelopackLocatorConfig(installed,updater,cache,manifest,binary,True)
        prefix='fault-'+mode+'/' if mode in ('delta','full','network') else ''
        manager=velopack.UpdateManager(velopack.HttpSource(config['feed_url']+prefix),velopack.UpdateOptions(False,10,config['channel']),locator)
        info=manager.check_for_updates()
        if not info:raise RuntimeError('Missing target in native fault case')
        if mode=='cached-corrupt':(cache/info.TargetFullRelease.FileName).write_bytes(b'bad')
        try:
            manager.download_updates(info)
            verify_cached(cache,info.TargetFullRelease)
            if mode in ('full','network','cached-corrupt'):raise AssertionError('Bad package accepted: '+mode)
            if mode=='no-base' and info.DeltasToTarget:raise AssertionError('Missing base did not choose full')
            if mode=='delta' and not info.DeltasToTarget:raise AssertionError('Invalid delta test did not attempt delta')
            results[mode]='verified full fallback'
        except (RuntimeError,ValueError) as exc:
            if mode not in ('full','network','cached-corrupt'):raise
            results[mode]='rejected: '+type(exc).__name__
    return results


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--bridge-version')
    parser.add_argument('--target-version')
    parser.add_argument('--reuse-payloads',action='store_true')
    args=parser.parse_args()
    project=Path(__file__).resolve().parents[1]
    output=project/'dist/installed-update'
    output.mkdir(parents=True,exist_ok=True)
    root=Path(tempfile.mkdtemp(prefix='shaq-installed-update-中文 space-')).resolve()
    reports=[]
    def run(label,command,timeout=1200):
        with (output/(label+'.log')).open('w',encoding='utf-8') as stream:
            env=dict(os.environ)
            if label.startswith('installed-'):
                for key in ('NO_PROXY','no_proxy'):
                    env[key]=','.join(filter(None,(env.get(key,''),'127.0.0.1','localhost')))
            result=subprocess.run([str(x) for x in command],stdout=stream,stderr=subprocess.STDOUT,timeout=timeout,env=env)
        reports.append({'stage':label,'returncode':result.returncode})
        if result.returncode:raise RuntimeError(label+' failed; see retained log')
    tools=json.loads((project/'packaging/updater-toolchain.json').read_text())
    args.bridge_version=args.bridge_version or tools['acceptance_bridge_version']
    args.target_version=args.target_version or tools['candidate_version']
    updates=json.loads((project/'config/software-updates.json').read_text())
    key=platform.system()+'/'+platform.machine().lower()
    channel=updates['channels'][key]
    feed=root/'feed'
    app_name='SHAQ Daily Oracle Lab'
    try:
        for stage,version in (('bridge',args.bridge_version),('candidate',args.target_version)):
            payload=project/'dist'/stage
            if not args.reuse_payloads:
                run(stage+'-build',[sys.executable,project/'packaging/build_desktop.py','--output',payload,'--version',version])
            app=payload/(app_name+('.app' if sys.platform=='darwin' else ''))
            run(stage+'-audit',[sys.executable,project/'packaging/audit_payload.py',app,'--output',output/(stage+'-audit.json')])
            run(stage+'-pack',[sys.executable,project/'packaging/build_desktop.py','--manage-existing',app,'--output',feed,'--version',version])
            if stage=='bridge':
                native=feed/(updates['package_id']+'-'+channel+'-Portable.zip') if sys.platform=='darwin' else feed/'SHAQ-Daily-Oracle-Lab-Windows-x64-Setup.exe'
                bridge_package=root/native.name
                shutil.copy2(native,bridge_package)
        (root/'installed').mkdir()
        if sys.platform=='darwin':
            run('bridge-unpack',['ditto','-xk',bridge_package,root/'dmg-source'])
            run('bridge-dmg',['hdiutil','create','-volname',app_name,'-srcfolder',root/'dmg-source','-format','UDZO',root/'bridge.dmg'])
            mount=root/'mount';mount.mkdir()
            run('bridge-mount',['hdiutil','attach','-nobrowse','-mountpoint',mount,root/'bridge.dmg'])
            try:run('bridge-install',['ditto',mount/(app_name+'.app'),root/'installed'/(app_name+'.app')])
            finally:run('bridge-unmount',['hdiutil','detach',mount])
            installed=root/'installed'/(app_name+'.app')
            executable=installed/'Contents/MacOS'/app_name
        else:
            installed=root/'installed'/app_name
            run('bridge-install',[bridge_package,'--silent','--installto',installed])
            executable=installed/'current'/(app_name+'.exe')
        cache=root/'packages';cache.mkdir()
        full=next(feed.glob('*-'+args.bridge_version+'-*-full.nupkg'))
        shutil.copy2(full,cache/full.name)
        handler=functools.partial(FaultHandler,directory=str(feed))
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        config={'bridge_version':args.bridge_version,'target_version':args.target_version,'package_id':updates['package_id'],
                'channel':channel,'feed_url':f'http://127.0.0.1:{server.server_port}/'}
        configuration=root/'acceptance.json';configuration.write_text(json.dumps(config))
        run('installed-bridge',[executable,'--update-smoke',configuration],timeout=180)
        deadline=time.monotonic()+180
        while time.monotonic()<deadline and not (root/'target-result.json').exists():time.sleep(.2)
        target=json.loads((root/'target-result.json').read_text())
        if target['status']!='passed':raise RuntimeError('Installed target GUI failed: '+str(target))
        run('installed-replay',[executable,'--update-smoke',configuration,'--update-stage','replay'],timeout=120)
        replay=json.loads((root/'replay-result.json').read_text())
        if replay['status']!='passed':raise RuntimeError('Installed restart/replay failed')
        run('installed-audit',[sys.executable,project/'packaging/audit_payload.py',installed,'--output',output/'installed-audit.json'])
        faults=native_download_faults(root,feed,installed,configuration)
        if sys.platform=='darwin':
            if installed.parent != root/'installed' or installed.is_symlink():raise RuntimeError('Unexpected uninstall target')
            shutil.rmtree(installed)
        else:run('uninstall',[installed/'Update.exe','uninstall','--silent'])
        if installed.exists():raise RuntimeError('Isolated native uninstall left the installed app')
        server.shutdown()
        assets=[]
        for file in feed.glob('*.nupkg'):
            with file.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
            assets.append({'name':file.name,'bytes':file.stat().st_size,'sha256':digest})
            shutil.copy2(file,output/file.name)
        shutil.copy2(feed/('releases.'+channel+'.json'),output/('releases.'+channel+'.json'))
        result={'status':'passed','platform':key,'root':str(root),'stages':reports,'assets':assets,'target':target,'replay':replay,
                'uninstalled':True,'native_sdk_download_faults':faults,'cache_files':sorted(p.name for p in cache.glob('*.nupkg')),
                'limitations':['explicit isolated App.run bypass','deterministic model substitute','one native upgrade, not ten successive upgrades']}
    except Exception as exc:
        result={'status':'failed','platform':key,'root':str(root),'stages':reports,'error':str(exc)}
    for file in root.glob('*-result.json'):shutil.copy2(file,output/file.name)
    (output/'acceptance.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result),flush=True)
    return 0 if result['status']=='passed' else 2


if __name__=='__main__':raise SystemExit(main())
