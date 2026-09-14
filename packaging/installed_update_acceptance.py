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


def transition_feed(feed, version):
    assets=[asset for asset in feed['Assets'] if asset['Version']==version]
    if not any(asset['Type']=='Full' for asset in assets):
        raise ValueError('Missing transition full package: '+version)
    return {**feed,'Assets':assets}


def verify_cache(cache, current):
    files=sorted(path.name for path in cache.glob('*.nupkg'))
    if files!=[current]:raise RuntimeError('Native cache retained obsolete packages: '+str(files))
    return files


def uninstall_windows(run, installed, project, output):
    run('uninstall',[installed/'Update.exe','uninstall','--silent'])
    # Update.exe schedules its own deletion after exit. Use the same bounded,
    # read-only completion probe as final Windows delivery, retaining leftovers.
    run('uninstall-check',[sys.executable,project/'packaging/verify_uninstall.py',installed,
        '--output',output/'uninstall-report.json'],timeout=45)


def retain_failed_package_comparison(expected, cached, output):
    """Failure-only diagnostics: entry identities, never extracted program data."""
    def describe(path):
        result={'filename':path.name,'exists':path.is_file(),'is_symlink':path.is_symlink()}
        if not result['exists'] or result['is_symlink']:
            return result
        try:
            with path.open('rb') as stream:result['sha256']=hashlib.file_digest(stream,'sha256').hexdigest()
            result['size']=path.stat().st_size
            result['entries']=[]
            with zipfile.ZipFile(path) as archive:
                for entry in archive.infolist():
                    with archive.open(entry) as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
                    result['entries'].append({'name':entry.filename,'size':entry.file_size,
                        'create_system':entry.create_system,'external_attr':entry.external_attr,'sha256':digest})
        except (OSError,ValueError,zipfile.BadZipFile) as exc:
            result['error']=str(exc)
        return result
    output.write_text(json.dumps({'expected':describe(expected),'cached':describe(cached)},indent=2),encoding='utf-8')


def verify_program_copies(installed, system):
    if system=='darwin':
        copies=sorted(path.name for path in installed.parent.glob('*.app'))
        expected=[installed.name]
    else:
        copies=sorted(path.name for path in installed.iterdir()
                      if path.is_dir() and (path.name=='current' or path.name.startswith('app-')))
        expected=['current']
    if copies!=expected:raise RuntimeError('Native install retained obsolete program copies: '+str(copies))
    return copies


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
    parser.add_argument('--prior-version')
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
    args.prior_version=args.prior_version or tools['acceptance_prior_version']
    args.target_version=args.target_version or tools['candidate_version']
    updates=json.loads((project/'config/software-updates.json').read_text())
    key=platform.system()+'/'+platform.machine().lower()
    channel=updates['channels'][key]
    feed=root/'feed'
    app_name='SHAQ Daily Oracle Lab'
    try:
        for stage,version in (('prior',args.prior_version),('bridge',args.bridge_version),('candidate',args.target_version)):
            payload=project/'dist'/stage
            if not args.reuse_payloads:
                run(stage+'-build',[sys.executable,project/'packaging/build_desktop.py','--output',payload,'--version',version])
            app=payload/(app_name+('.app' if sys.platform=='darwin' else ''))
            run(stage+'-audit',[sys.executable,project/'packaging/audit_payload.py',app,'--output',output/(stage+'-audit.json')])
            run(stage+'-pack',[sys.executable,project/'packaging/build_desktop.py','--manage-existing',app,'--output',feed,'--version',version])
            if stage=='prior':
                native=feed/(updates['package_id']+'-'+channel+'-Portable.zip') if sys.platform=='darwin' else feed/'SHAQ-Daily-Oracle-Lab-Windows-x64-Setup.exe'
                bridge_package=root/native.name
                shutil.copy2(native,bridge_package)
        (root/'installed').mkdir()
        if sys.platform=='darwin':
            run('bridge-unpack',['ditto','-xk',bridge_package,root/'dmg-source'])
            run('bridge-dmg',[sys.executable,project/'packaging/create_dmg.py','--volume',app_name,'--source',root/'dmg-source','--output',root/'bridge.dmg'])
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
        full=next(feed.glob('*-'+args.prior_version+'-*-full.nupkg'))
        shutil.copy2(full,cache/full.name)
        handler=functools.partial(FaultHandler,directory=str(feed))
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        from shaq_daily_oracle.update_smoke import wait_event,process_running
        from shaq_daily_oracle.settings import _atomic_json
        manifest=feed/('releases.'+channel+'.json')
        complete_feed=json.loads(manifest.read_text())
        transitions=[]
        for number,(old,new) in enumerate(((args.prior_version,args.bridge_version),(args.bridge_version,args.target_version)),1):
            selected=transition_feed(complete_feed,new)
            _atomic_json(manifest,selected)
            current=next(asset['FileName'] for asset in selected['Assets'] if asset['Type']=='Full')
            # Seed only recognized updater-owned package names; the actual SDK
            # owns cleanup. Never delete cache files from the acceptance driver.
            obsolete=[]
            for version in ((args.prior_version,) if number>1 else ()):
                stale=next(feed.glob('*-'+version+'-*-full.nupkg'))
                shutil.copy2(stale,cache/stale.name);obsolete.append(stale.name)
            config={'bridge_version':old,'target_version':new,'package_id':updates['package_id'],
                    'channel':channel,'feed_url':f'http://127.0.0.1:{server.server_port}/',
                    'events_directory':'events-'+str(number)}
            configuration=root/('acceptance-'+str(number)+'.json');_atomic_json(configuration,config)
            events=root/config['events_directory']
            run('installed-bridge-'+str(number),[executable,'--update-smoke',configuration],timeout=300)
            health=wait_event(events/'target-health.json',timeout=180)
            time.sleep(2)
            if not process_running(health['pid']):raise RuntimeError('Target GUI exited after health')
            _atomic_json(events/'close-target.json',{'status':'passed'})
            target=wait_event(events/'target-result.json',timeout=60)
            if target['status']!='passed':raise RuntimeError('Installed target GUI failed: '+str(target))
            deadline=time.monotonic()+30
            while process_running(health['pid']) and time.monotonic()<deadline:time.sleep(.1)
            if process_running(health['pid']):raise RuntimeError('Target acceptance GUI did not close')
            cached=verify_cache(cache,current)
            copies=verify_program_copies(installed,sys.platform)
            peer=wait_event(events/'peer-result.json')
            if not peer.get('cooperatively_closed'):raise RuntimeError('Missing cooperative second GUI close')
            transitions.append({'from':old,'to':new,'target':target,'peer':peer,
                                'seeded_obsolete':obsolete,'cache_files':cached,'program_copies':copies,'events':str(events)})
        run('installed-replay',[executable,'--update-smoke',configuration,'--update-stage','replay'],timeout=120)
        replay=wait_event(events/'replay-result.json')
        if replay['status']!='passed':raise RuntimeError('Installed restart/replay failed')
        run('installed-audit',[sys.executable,project/'packaging/audit_payload.py',installed,'--output',output/'installed-audit.json'])
        faults=native_download_faults(root,feed,installed,configuration)
        if sys.platform=='darwin':
            if installed.parent != root/'installed' or installed.is_symlink():raise RuntimeError('Unexpected uninstall target')
            shutil.rmtree(installed)
        else:uninstall_windows(run,installed,project,output)
        if installed.exists():raise RuntimeError('Isolated native uninstall left the installed app')
        server.shutdown()
        assets=[]
        for file in feed.glob('*.nupkg'):
            with file.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
            assets.append({'name':file.name,'bytes':file.stat().st_size,'sha256':digest})
            shutil.copy2(file,output/file.name)
        shutil.copy2(feed/('releases.'+channel+'.json'),output/('releases.'+channel+'.json'))
        result={'status':'passed','platform':key,'root':str(root),'stages':reports,'assets':assets,'target':target,'replay':replay,'transitions':transitions,
                'uninstalled':True,'native_sdk_download_faults':faults,'cache_files':sorted(p.name for p in cache.glob('*.nupkg')),
                'limitations':['explicit isolated App.run bypass','deterministic model substitute','two native transitions plus seeded stale cache; not a ten-version soak test']}
    except Exception as exc:
        result={'status':'failed','platform':key,'root':str(root),'stages':reports,'error':str(exc)}
        if 'current' in locals():
            try:
                retain_failed_package_comparison(feed/current,root/'packages'/current,output/'failed-package-comparison.json')
            except Exception as diagnostic_error:
                result['package_diagnostic_error']=str(diagnostic_error)
    for file in root.glob('*-result.json'):shutil.copy2(file,output/file.name)
    for directory in root.glob('events-*'):
        shutil.copytree(directory,output/directory.name,dirs_exist_ok=True)
    (output/'acceptance.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result),flush=True)
    return 0 if result['status']=='passed' else 2


if __name__=='__main__':raise SystemExit(main())
