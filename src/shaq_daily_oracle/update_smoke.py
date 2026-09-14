"""Explicit full-native installed acceptance, never the production update feed.

Model calls are the existing deterministic two-method lab fixture. Download,
delta synthesis, process exit, replacement and restart are actual Velopack.
"""
import argparse
from contextlib import nullcontext
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request

from .app_paths import app_paths, application_version
from .settings import _atomic_json


def wait_event(path, *, timeout=90, failure_path=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if failure_path is not None and failure_path.exists():
            failure = json.loads(failure_path.read_text(encoding='utf-8'))
            if failure.get('status') == 'failed':
                raise RuntimeError(str(failure))
        if path.exists():
            value = json.loads(path.read_text(encoding='utf-8'))
            if value.get('status') == 'failed':
                raise RuntimeError(str(value))
            return value
        time.sleep(.1)
    raise TimeoutError('Missing installed acceptance event: ' + path.name)


def wait_restart_confirmation(path, version, installation_id, *, deadline):
    """Observe real async GUI confirmation within the original render budget."""
    while time.monotonic() < deadline:
        try:
            receipt = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            receipt = {}
        if (receipt.get('version') == version and receipt.get('installation_id') == installation_id
                and receipt.get('completed_at')):
            return receipt
        time.sleep(.1)
    raise TimeoutError('Missing GUI-health update confirmation for current installation')


def verify_deferred(state, before, after, *, dirty):
    message = ('请先保存或放弃所有窗口中尚未保存的方法或连接修改，再更新。'
               if dirty else '已下载，等待本地任务运行完更新')
    flag = 'waiting_for_edits' if dirty else 'waiting_for_idle'
    if state.get('status') != 'ready' or state.get(flag) is not True or state.get('message') != message:
        raise RuntimeError('Update did not remain ready with expected waiting message: ' + str(state))
    if before != after:
        raise RuntimeError('Installed program was replaced while update was deferred')


def program_hashes():
    executable = Path(sys.executable)
    files = [executable, executable.parent/'sq.version']
    return {file.name: hashlib.sha256(file.read_bytes()).hexdigest() for file in files if file.is_file()}


def process_running(pid):
    # os.kill(pid, 0) is NOT a harmless probe on Windows.
    if sys.platform == 'win32':
        import csv
        rows = csv.reader(subprocess.check_output(
            ['tasklist', '/FI', f'PID eq {pid}', '/FO', 'CSV', '/NH'], text=True).splitlines())
        return any(len(row) > 1 and row[1] == str(pid) for row in rows)
    status = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True).stdout.strip()
    return bool(status) and not status.startswith('Z')


def acceptance_paths(root, executable, *, ensure=True):
    root = root.resolve()
    allowed = {Path(tempfile.gettempdir()).resolve(), Path('/tmp').resolve()}
    if root.parent not in allowed or not root.name.startswith('shaq-installed-update-'):
        raise ValueError('Acceptance requires a dedicated temporary installation')
    if not executable.resolve().is_relative_to(root/'installed'):
        raise ValueError('Acceptance cannot run against the normal installed application')
    original = app_paths()
    data = root/'data'
    research = data/'research'
    paths=replace(original, data_root=data,config_root=data/'config',log_root=data/'logs',
                   runtime_root=data/'runtime',dashboard_db=data/'dashboard.sqlite3',
                   settings_file=data/'config/settings.json',effective_ai_config=data/'config/ai-backend.json',
                   research_root=research,batches_root=research/'batches',skill_registry_root=research/'skill_versions',
                   research_database=research/'index.sqlite3',research_settings_file=data/'config/research-settings.json')
    return paths.ensure() if ensure else paths


def hashes(paths):
    from .minute_settlements import MINUTE_NAMESPACE
    targets = [paths.config_root, paths.skill_registry_root, paths.batches_root,
               paths.research_root/'virtual_accounts', paths.research_root/MINUTE_NAMESPACE,
               paths.data_root/'software-update-preferences.json']
    files = [file for target in targets for file in (target.rglob('*') if target.is_dir() else [target]) if file.is_file()]
    return {str(file.relative_to(paths.data_root)):hashlib.sha256(file.read_bytes()).hexdigest() for file in files}


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--update-smoke',type=Path,required=True)
    parser.add_argument('--update-stage',choices=('bridge','peer','target','replay'),default='bridge')
    args=parser.parse_args(argv)
    config=json.loads(args.update_smoke.read_text())
    root=args.update_smoke.parent.resolve()
    paths=acceptance_paths(root,Path(sys.executable),ensure=False)
    event_name=config.get('events_directory','events')
    if Path(event_name).name!=event_name or event_name in ('.','..'):
        raise ValueError('Coordination directory must be inside the isolated installation')
    events=root/event_name
    if events.is_symlink():raise ValueError('Coordination directory cannot be a symlink')
    events.mkdir(exist_ok=True)
    stage=args.update_stage
    old_processes=[]
    if stage=='target':
        old=wait_event(events/'old-processes.json')['pids']
        old_processes=[{'pid':pid,'running':process_running(pid)} for pid in old]
        if any(p['running'] for p in old_processes):
            raise RuntimeError('Old GUI process survived before target data access')
    paths.ensure()
    version=application_version(paths.package_root)
    expected=config['bridge_version'] if stage in ('bridge','peer') else config['target_version']
    if version != expected:raise ValueError('Actual installed package version mismatch')
    from .desktop import DesktopBridge,desktop_api,_bind_gui_smoke_fixture
    from .update_admission import gate_for
    from .update_gui import GuiSession
    from .lab_smoke import run_lab_smoke
    from .research_settings import default_research_settings
    import velopack
    import webview
    if stage=='bridge' and not (root/'fixture.json').exists():
        fixture=run_lab_smoke(package_root=paths.package_root,output_root=paths.data_root)
        if fixture['status']!='passed':raise RuntimeError('Whole-app research fixture failed')
        _atomic_json(root/'fixture.json',fixture)
        settings=default_research_settings(paths.package_root)
        settings.update(automatic_run_enabled=False,model_profiles=[{'profile_id':'synthetic-local','protocol':'codex-cli','model':'acceptance-deterministic-substitute'}])
        _atomic_json(paths.research_settings_file,settings)
        _atomic_json(paths.settings_file,{'automatic_run_enabled':False,'synthetic_acceptance':True})
        _atomic_json(paths.data_root/'software-update-preferences.json',{'automatic_enabled':True})
    else:
        fixture=json.loads((root/'fixture.json').read_text())
        if hashes(paths)!=json.loads((root/'before-hashes.json').read_text()):
            raise RuntimeError('Saved research, balances, methods, drafts or settings changed during upgrade')
    gate=gate_for(paths)
    permit=gate.target_startup(version) if stage=='target' else nullcontext()
    with permit:
        bridge=DesktopBridge(paths)
        runtime=bridge._software_updater()
        # The acceptance feed is local and explicit; never run production
        # automatic GitHub checks against synthetic research settings.
        runtime.start_automatic_checks=lambda:None
        _bind_gui_smoke_fixture(bridge,fixture['browser_state'],fixture['batch_detail'])
        if stage=='bridge' and not (root/'draft.json').exists():
            method=fixture['methods'][0]
            _atomic_json(root/'draft.json',bridge.lab.copy_local_version(version_id=method['version_id'],author=method['author']))
            _atomic_json(root/'before-hashes.json',hashes(paths))
        page=Path(__file__).with_name('desktop')/'index.html'
        window=webview.create_window('SHAQ installed update acceptance',page.as_uri(),js_api=desktop_api(bridge),width=1320,height=860)
        bridge.window=window
        result={'status':'failed','stage':stage,'actual_version':version,'model':'existing deterministic lab_smoke substitute',
                'pid':os.getpid(),
                'engine':'real Velopack 1.2.0 native SDK','production_autolocator':'intentionally bypassed for isolated acceptance'}
        peer=None
        def inspect():
            try:
                deadline=time.monotonic()+40
                while time.monotonic()<deadline:
                    if window.evaluate_js("Boolean(window.pywebview && document.querySelector('#run').textContent.trim())"):
                        break
                    time.sleep(.1)
                else:raise RuntimeError('Installed native GUI did not render')
                for name in ('run','editor','history'):
                    window.evaluate_js(f"showPage('{name}')")
                    if not window.evaluate_js(f"Boolean(document.querySelector('#{name}').textContent.trim())"):
                        raise RuntimeError('Installed native page did not render: '+name)
                result['pages']=['run','editor','history']
                result['preserved_hashes']=hashes(paths)==json.loads((root/'before-hashes.json').read_text())
                if not result['preserved_hashes']:raise RuntimeError('Saved user records changed')
                if stage=='peer':
                    for kind in ('analysis','settlement','background'):
                        with bridge._runtime_admission.work():
                            _atomic_json(events/(kind+'-held.json'),{'status':'passed','pid':os.getpid(),'kind':kind})
                            wait_event(events/(kind+'-release.json'))
                            _atomic_json(events/(kind+'-completed.json'),{'status':'passed','pid':os.getpid(),'kind':kind,'completed':True})
                        _atomic_json(events/(kind+'-released.json'),{'status':'passed'})
                    # Use the existing code editor and real save button. No
                    # connection credentials (even synthetic ones) are entered.
                    draft=json.loads((root/'draft.json').read_text())['draft_id']
                    window.evaluate_js("showPage('editor');q('#draft-id').value="+json.dumps(draft)+";wb.draft="+json.dumps(draft)+";q('[data-module=screening]').click()")
                    deadline=time.monotonic()+30
                    while time.monotonic()<deadline:
                        if window.evaluate_js("Boolean(q('#module-code')?.value && q('#module-cases')?.value)"):break
                        time.sleep(.1)
                    else:raise RuntimeError('Peer method editor did not load')
                    window.evaluate_js("window.acceptanceOriginalCode=q('#module-code').value;q('#module-code').value+='\\n// temporary unsaved acceptance edit';q('#module-code').dispatchEvent(new Event('input',{bubbles:true}))")
                    _atomic_json(events/'peer-dirty.json',{'status':'passed','pid':os.getpid(),'field':'module-code'})
                    wait_event(events/'save-peer.json')
                    window.evaluate_js("q('#module-code').value=window.acceptanceOriginalCode;q('#module-code').dispatchEvent(new Event('input',{bubbles:true}));q('#save-module-code').click()")
                    deadline=time.monotonic()+30
                    while time.monotonic()<deadline:
                        if window.evaluate_js("q('#module-code-status').textContent.includes('项测试通过，已保存')"):break
                        time.sleep(.1)
                    else:raise RuntimeError('Existing method save flow did not succeed')
                    if hashes(paths)!=json.loads((root/'before-hashes.json').read_text()):
                        raise RuntimeError('No-op method save changed preserved fixture bytes')
                    _atomic_json(events/'peer-saved.json',{'status':'passed','pid':os.getpid(),'preserved_hashes':True})
                    # Only GuiSession's native close request may close this old
                    # window. A harness timeout is failure, never success.
                    wait_event(events/'admitted.json')
                    return
                if stage=='bridge':
                    from .update_native import native_locator,verify_cached
                    from .software_updates import UpdateRuntime
                    from urllib.parse import urlparse
                    url=config['feed_url']; parsed=urlparse(url)
                    if parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost'):
                        raise ValueError('Acceptance feed must be loopback HTTP')
                    locator,cache=native_locator(velopack,config['package_id'],cache=root/'packages')
                    manager=velopack.UpdateManager(velopack.HttpSource(url),velopack.UpdateOptions(False,10,config['channel']),locator)
                    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url+'releases.'+config['channel']+'.json') as response:
                        feed=json.load(response)
                    for asset in feed['Assets']:UpdateRuntime._validate_asset(asset,config['package_id'],config['channel'],config['target_version'])
                    info=manager.check_for_updates()
                    if not info or not info.DeltasToTarget:raise RuntimeError('Actual native SDK did not select a delta')
                    result['delta_count']=len(info.DeltasToTarget)
                    result['native_target']=info.TargetFullRelease.Version
                    manager.download_updates(info)
                    content_sha=next(a['ContentSHA256'] for a in feed['Assets'] if a['FileName']==info.TargetFullRelease.FileName)
                    verify_cached(cache,info.TargetFullRelease,content_sha256=content_sha)
                    result['download_verified']=True
                    runtime._manager=manager;runtime._info=info;runtime._native_cache=cache
                    runtime._target_content_sha=content_sha
                    runtime._state.update(status='ready',latest_version=config['target_version'])
                    original_program=program_hashes()
                    peer_log=(events/'peer.log').open('w',encoding='utf-8')
                    peer=subprocess.Popen([sys.executable,'--update-smoke',str(args.update_smoke),'--update-stage','peer'],stdout=peer_log,stderr=subprocess.STDOUT)
                    _atomic_json(events/'old-processes.json',{'status':'passed','pids':[os.getpid(),peer.pid]})
                    result['peer_pid']=peer.pid
                    result['waiting_stages']=[]
                    for kind in ('analysis','settlement','background'):
                        held=wait_event(events/(kind+'-held.json'))
                        if held['pid']!=peer.pid:raise RuntimeError('Work was not held in the second installed process')
                        state=runtime.apply(method='automatic')
                        verify_deferred(state,original_program,program_hashes(),dirty=False)
                        evidence={'status':'passed','state':state,'program_unchanged':True,'peer_alive':peer.poll() is None}
                        if not evidence['peer_alive']:raise RuntimeError('Peer closed during active work')
                        _atomic_json(events/(kind+'-waiting.json'),evidence)
                        result['waiting_stages'].append(kind)
                        _atomic_json(events/(kind+'-release.json'),{'status':'passed'})
                        wait_event(events/(kind+'-released.json'))
                    wait_event(events/'peer-dirty.json')
                    state=runtime.automatic_step()
                    verify_deferred(state,original_program,program_hashes(),dirty=True)
                    if peer.poll() is not None:raise RuntimeError('Dirty second GUI closed')
                    _atomic_json(events/'dirty-waiting.json',{'status':'passed','state':state,'peer_alive':True,'program_unchanged':True})
                    _atomic_json(events/'save-peer.json',{'status':'passed'})
                    wait_event(events/'peer-saved.json')
                    # Propagate the dedicated isolated target entry through the
                    # SDK, not a parent-Python replacement simulation.
                    class Restart:
                        def apply_updates_and_restart(self, update):
                            _atomic_json(events/'admitted.json',{'status':'passed','owner_pid':os.getpid(),'peer_pid':peer.pid})
                            peer.wait(timeout=30)
                            if peer.returncode!=0:raise RuntimeError('Second old GUI did not close cooperatively')
                            manager.apply_updates_and_restart_with_args(update,['--update-smoke',str(args.update_smoke),'--update-stage','target'])
                    runtime._manager=Restart()
                    result['status']='applying'
                    _atomic_json(events/(stage+'-result.json'),result)
                    runtime.automatic_step()
                    raise RuntimeError('Native SDK apply unexpectedly returned')
                else:
                    # Rendering precedes the async confirm_desktop_ready API.
                    # A prior transition's receipt is not failure or success:
                    # wait for this generation without resetting the GUI budget.
                    receipt=wait_restart_confirmation(paths.data_root/'software-update-history.json',
                        version,bridge._runtime_admission.history,deadline=deadline)
                    result['last_update']=receipt
                    if stage=='target':
                        result['old_processes_before_data_access']=old_processes
                        _atomic_json(events/'target-health.json',{'status':'passed','pid':os.getpid(),'version':version,'receipt':receipt})
                        wait_event(events/'close-target.json')
                        if not window.evaluate_js("Boolean(document.querySelector('#history').textContent.trim())"):
                            raise RuntimeError('Target GUI did not remain open after health')
                        result['remained_open_after_health']=True
                    result['status']='passed'
            except Exception as exc:
                result['error']=str(exc)
            finally:
                if stage!='peer' or result.get('error'):
                    _atomic_json(events/(stage+'-result.json'),result)
                    window.destroy()
        with GuiSession(gate.root,window,admission=bridge._runtime_admission) as session:
            runtime.gui_session=session
            webview.start(inspect,debug=False,private_mode=True)
        if stage=='peer' and not result.get('error'):
            result.update(status='passed',cooperatively_closed=True)
            _atomic_json(events/(stage+'-result.json'),result)
    return 0 if result['status']=='passed' else 2
