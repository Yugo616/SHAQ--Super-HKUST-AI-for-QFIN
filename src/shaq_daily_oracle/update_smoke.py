"""Explicit full-native installed acceptance, never the production update feed.

Model calls are the existing deterministic two-method lab fixture. Download,
delta synthesis, process exit, replacement and restart are actual Velopack.
"""
import argparse
from contextlib import nullcontext
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import urllib.request

from .app_paths import app_paths, application_version
from .settings import _atomic_json


def acceptance_paths(root, executable):
    root = root.resolve()
    allowed = {Path(tempfile.gettempdir()).resolve(), Path('/tmp').resolve()}
    if root.parent not in allowed or not root.name.startswith('shaq-installed-update-'):
        raise ValueError('Acceptance requires a dedicated temporary installation')
    if not executable.resolve().is_relative_to(root/'installed'):
        raise ValueError('Acceptance cannot run against the normal installed application')
    original = app_paths()
    data = root/'data'
    research = data/'research'
    return replace(original, data_root=data,config_root=data/'config',log_root=data/'logs',
                   runtime_root=data/'runtime',dashboard_db=data/'dashboard.sqlite3',
                   settings_file=data/'config/settings.json',effective_ai_config=data/'config/ai-backend.json',
                   research_root=research,batches_root=research/'batches',skill_registry_root=research/'skill_versions',
                   research_database=research/'index.sqlite3',research_settings_file=data/'config/research-settings.json').ensure()


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
    parser.add_argument('--update-stage',choices=('bridge','target','replay'),default='bridge')
    args=parser.parse_args(argv)
    config=json.loads(args.update_smoke.read_text())
    root=args.update_smoke.parent.resolve()
    paths=acceptance_paths(root,Path(sys.executable))
    stage=args.update_stage
    version=application_version(paths.package_root)
    expected=config['bridge_version'] if stage=='bridge' else config['target_version']
    if version != expected:raise ValueError('Actual installed package version mismatch')
    from .desktop import DesktopBridge,desktop_api,_bind_gui_smoke_fixture
    from .update_admission import gate_for
    from .update_gui import GuiSession
    from .lab_smoke import run_lab_smoke
    from .research_settings import default_research_settings
    import velopack
    import webview
    if stage=='bridge':
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
        if stage=='bridge':
            method=fixture['methods'][0]
            bridge.lab.copy_local_version(version_id=method['version_id'],author=method['author'])
            _atomic_json(root/'before-hashes.json',hashes(paths))
        page=Path(__file__).with_name('desktop')/'index.html'
        window=webview.create_window('SHAQ installed update acceptance',page.as_uri(),js_api=desktop_api(bridge),width=1320,height=860)
        bridge.window=window
        result={'status':'failed','stage':stage,'actual_version':version,'model':'existing deterministic lab_smoke substitute',
                'engine':'real Velopack 1.2.0 native SDK','production_autolocator':'intentionally bypassed for isolated acceptance'}
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
                    # Propagate the dedicated isolated target entry through the
                    # SDK, not a parent-Python replacement simulation.
                    class Restart:
                        def apply_updates_and_restart(self, update):
                            manager.apply_updates_and_restart_with_args(update,['--update-smoke',str(args.update_smoke),'--update-stage','target'])
                    runtime._manager=Restart()
                    result['status']='applying'
                    _atomic_json(root/(stage+'-result.json'),result)
                    runtime.apply(method='automatic')
                    raise RuntimeError('Native SDK apply unexpectedly returned')
                else:
                    receipt=json.loads((paths.data_root/'software-update-history.json').read_text())
                    if receipt['version']!=version or not receipt['completed_at']:raise RuntimeError('Missing GUI-health update confirmation')
                    result['last_update']=receipt
                    result['status']='passed'
            except Exception as exc:
                result['error']=str(exc)
            finally:
                _atomic_json(root/(stage+'-result.json'),result)
                window.destroy()
        with GuiSession(gate.root,window) as session:
            runtime.gui_session=session
            webview.start(inspect,debug=False,private_mode=True)
    return 0 if result['status']=='passed' else 2
