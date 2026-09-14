"""Trusted release discovery and explicitly requested native updates."""
import json
import platform
import re
import threading
import time
from datetime import datetime, timezone

import httpx
from packaging.version import Version, InvalidVersion

from .app_paths import application_version


# Published asset names are a distribution contract, not a developer-machine path.
CHANNELS = {
    ('Darwin', 'arm64'): ('macos', 'SHAQ-Daily-Oracle-Lab-macOS-Apple-Silicon.dmg', 'macOS · Apple Silicon'),
    ('Darwin', 'x86_64'): ('macos', 'SHAQ-Daily-Oracle-Lab-macOS-Intel.dmg', 'macOS · Intel'),
    ('Windows', 'amd64'): ('windows', 'SHAQ-Daily-Oracle-Lab-Windows-x64-Setup.exe', 'Windows · x64'),
    ('Windows', 'x86_64'): ('windows', 'SHAQ-Daily-Oracle-Lab-Windows-x64-Setup.exe', 'Windows · x64'),
}


def select_release(rows, *, current, repository, system, machine):
    value={'current_version':current,'mode':'installer_only','latest_version':None,
           'release_url':None,'asset_url':None,'notes':'','status':'no_release'}
    channel=CHANNELS.get((system,machine.lower()))
    if channel is None:return {**value,'status':'unsupported','platform':f'{system} {machine}'}
    suffix,asset_name,label=channel
    value['platform']=label
    candidates=[]
    for row in rows:
        # Lab installers are intentionally published as internal-test releases.
        if row.get('draft'):continue
        match=re.fullmatch(r'lab-v(\d+\.\d+\.\d+)-'+suffix,str(row.get('tag_name','')))
        if not match:continue
        tag=row['tag_name']
        prefix=f'https://github.com/{repository}/releases/'
        page=prefix+'tag/'+tag
        asset=next((item for item in row.get('assets',[]) if item.get('name')==asset_name),None)
        if row.get('html_url')!=page or not asset:continue
        url=prefix+'download/'+tag+'/'+asset_name
        if asset.get('browser_download_url')!=url:continue
        candidates.append((Version(match[1]),row,asset,url,page))
    if not candidates:return value
    version,row,asset,url,page=max(candidates,key=lambda item:item[0])
    try:local=Version(current)
    except InvalidVersion:return {**value,'status':'unknown_local_version'}
    return {**value,'latest_version':str(version),'release_url':page,'asset_url':url,
            'internal_test_release':bool(row.get('prerelease')),
            'size_bytes':asset.get('size'),'notes':str(row.get('body') or '')[:12000],
            'status':'available' if version>local else 'current' if version==local else 'local_newer'}


def check_releases(package_root, *, client=None):
    config=json.loads((package_root/'config/team-repository.json').read_text(encoding='utf-8'))
    owner,repo=config['owner'],config['repository']
    if not all(re.fullmatch(r'[A-Za-z0-9_.-]+',value) for value in (owner,repo)):
        raise ValueError('软件发布仓库配置不正确')
    repository=f'{owner}/{repo}'
    request=client or httpx
    response=request.get(f'https://api.github.com/repos/{repository}/releases',
                         params={'per_page':100},timeout=20,
                         headers={'Accept':'application/vnd.github+json'})
    if response.status_code in (403,429):raise ValueError('GitHub 暂时限流，请稍后检查更新。')
    response.raise_for_status()
    rows=response.json()
    if not isinstance(rows,list):raise ValueError('GitHub 发布列表格式异常')
    return {**select_release(rows,current=application_version(package_root),repository=repository,
                            system=platform.system(),machine=platform.machine()),
            'checked_at':datetime.now(timezone.utc).isoformat()}


def read_update_feed(url):
    response = httpx.get(url, timeout=20, follow_redirects=True)
    response.raise_for_status()
    return response.json()


class UpdateRecovery:
    """Read-only UI for an interrupted replacement; never guesses a stale lock."""
    def __init__(self, paths):
        self.paths = paths

    def status(self):
        marker = self.paths.data_root / 'update-admission/installing.json'
        pending = json.loads(marker.read_text(encoding='utf-8'))
        version = pending.get('target_version', '')
        config = json.loads((self.paths.package_root / 'config/team-repository.json').read_text(encoding='utf-8'))
        current = application_version(self.paths.package_root)
        result = {'current_version':current, 'target_version':str(version), 'installer_url':''}
        channel = CHANNELS.get((platform.system(), platform.machine().lower()))
        if channel and re.fullmatch(r'\d+\.\d+\.\d+', str(version)) and all(
                re.fullmatch(r'[A-Za-z0-9_.-]+', config[key]) for key in ('owner','repository')):
            suffix, name, _ = channel
            result['installer_url'] = f"https://github.com/{config['owner']}/{config['repository']}/releases/download/lab-v{version}-{suffix}/{name}"
        return result

    def open_installer(self):
        import webbrowser
        url = self.status()['installer_url']
        if not url:
            raise ValueError('无法验证恢复安装包地址，请联系发布者。')
        return bool(webbrowser.open(url))


def launch_update_recovery(paths):
    import webview
    # No DashboardIndex, SettingsStore, or LabService is created on this path.
    html = '''<!doctype html><meta charset="utf-8"><title>更新恢复</title>
    <style>body{font:17px system-ui;padding:36px;line-height:1.7;color:#243047}button{font:inherit;padding:10px;margin-right:12px}</style>
    <h2>软件更新尚未完成</h2><p id="versions"></p>
    <p>为保护研究资料，分析、结算和后台写入已暂停。请先等待正在进行的安装完成。
    若安装已中断，请下载下方官方完整安装包，关闭此窗口后重新安装目标版本，再打开应用。
    不需要删除资料、凭据或设置。</p>
    <button onclick="openInstaller()">下载官方目标版本安装包</button><button onclick="refresh()">重新检查状态</button>
    <p id="status" role="status"></p><script>
    async function refresh(){try{const s=await pywebview.api.status();
    document.querySelector('#versions').textContent='当前 '+s.current_version+' → 目标 '+s.target_version;
    document.querySelector('#status').textContent=s.current_version===s.target_version?'目标版本已就位，请关闭窗口后重新打开应用。':'仍在等待目标版本安装完成。';
    }catch(e){document.querySelector('#status').textContent='无法读取更新状态，请联系发布者；研究资料未删除。';}}
    async function openInstaller(){try{await pywebview.api.open_installer();}catch(e){document.querySelector('#status').textContent=String(e);}}
    window.addEventListener('pywebviewready',refresh);</script>'''
    webview.create_window('SHAQ Lab · 更新恢复', html=html, js_api=UpdateRecovery(paths), width=760, height=500)
    webview.start()
    return 0


class UpdateRuntime:
    def __init__(self, paths, *, sdk=None):
        from .update_admission import WorkerAdmission
        self.paths, self.sdk = paths, sdk
        self._runtime_admission = WorkerAdmission(paths)
        self._lock = threading.RLock()
        self._state = {'status': 'unchecked', 'mode': 'installer_only'}
        self._manager = self._info = None
        self._last_attempt = 0

    def _read_local(self, name, default):
        try:
            return json.loads((self.paths.data_root / name).read_text(encoding='utf-8'))
        except FileNotFoundError:
            return default

    def status(self):
        with self._lock:
            return {**self._state,
                    'current_version':application_version(self.paths.package_root),
                    'automatic_enabled':self._read_local('software-update-preferences.json', {}).get('automatic_enabled') is True,
                    'last_update':self._read_local('software-update-history.json', None),
                    'target_version':self._state.get('latest_version') or self._read_local('software-update-check.json', {}).get('target_version'),
                    'last_checked_at':self._read_local('software-update-check.json', {}).get('checked_at')}

    def set_automatic(self, enabled):
        from .settings import _atomic_json
        from .update_admission import gate_for
        if type(enabled) is not bool:
            raise ValueError('自动更新开关必须为开或关')
        with self._lock, self._runtime_admission.work():
            _atomic_json(self.paths.data_root / 'software-update-preferences.json', {'automatic_enabled':enabled})
        return self.status()

    def automatic_step(self):
        from .update_admission import UpdateBusy
        with self._lock:
            self._runtime_admission.assert_current()
            if self._state.get('queued_apply_method') == 'manual' and self._state['status'] == 'ready':
                if self._state.get('queued_apply_target_version') != self._state.get('latest_version'):
                    raise ValueError('等待更新的目标版本发生变化，请重新确认')
                return self.apply(method='manual')
            if not self.status()['automatic_enabled']:
                return self.status()
            state = self._state['status']
            if state == 'ready':
                return self.apply(method='automatic')
            if state == 'available' and self._state.get('mode') == 'managed':
                return self.download()
            if state in {'downloading','applying'}:
                return self.status()
            config = json.loads((self.paths.package_root / 'config/software-updates.json').read_text(encoding='utf-8'))
            if time.monotonic() - self._last_attempt >= config['automatic_check_interval_seconds']:
                self._last_attempt = time.monotonic()
                self.check()
                if self._state['status'] == 'available' and self._state.get('mode') == 'managed':
                    return self.download()
            return self.status()

    def start_automatic_checks(self):
        if hasattr(self, '_automatic_thread'):
            return
        config = json.loads((self.paths.package_root / 'config/software-updates.json').read_text(encoding='utf-8'))
        def run():
            while True:
                try:
                    self.automatic_step()
                except Exception:
                    with self._lock:
                        self._state['message'] = '自动更新未完成；当前版本和资料保留，请在软件更新中重试。'
                time.sleep(config['automatic_idle_poll_seconds'])
        self._automatic_thread = threading.Thread(target=run, daemon=True, name='shaq-auto-update')
        self._automatic_thread.start()

    def check(self):
        with self._lock, self._runtime_admission.work():
            if self._state['status'] in {'downloading', 'ready', 'applying'}:
                return self.status()
            self._manager = self._info = None
            self._state = {**self._state, 'status': 'checking'}
            try:
                self._check()
                from .settings import _atomic_json
                from .update_admission import gate_for
                with gate_for(self.paths).work():
                    _atomic_json(self.paths.data_root / 'software-update-check.json', {
                        'checked_at':datetime.now(timezone.utc).isoformat(),
                        'target_version':self._state.get('latest_version'),
                    })
                return self.status()
            except Exception:
                self._state = {**self._state, 'status': 'check_failed',
                               'message': '未能验证更新，请检查网络后重试。'}
                raise

    def _check(self):
        selected = check_releases(self.paths.package_root)
        self._state = {**selected, 'message': '旧安装或开发模式：仅提供完整安装包。请关闭应用后安装一次以启用应用内更新。'}
        config = json.loads((self.paths.package_root / 'config/software-updates.json').read_text(encoding='utf-8'))
        channel = config['channels'].get(f'{platform.system()}/{platform.machine().lower()}')
        if not channel or selected['status'] not in {'available','current','local_newer'}:
            return self.status()
        repo_config = json.loads((self.paths.package_root / 'config/team-repository.json').read_text(encoding='utf-8'))
        repository = f"{repo_config['owner']}/{repo_config['repository']}"
        suffix = CHANNELS[(platform.system(), platform.machine().lower())][0]
        version = selected['latest_version']
        if not re.fullmatch(r'\d+\.\d+\.\d+', version):
            raise ValueError('更新版本格式无效')
        base = f'https://github.com/{repository}/releases/download/lab-v{version}-{suffix}/'
        try:
            sdk = self.sdk
            locator_args = {}
            if sdk is None:
                import velopack as sdk
                from .update_native import native_locator
                locator, self._native_cache = native_locator(sdk, config['package_id'])
                locator_args['locator'] = locator
            manager = sdk.UpdateManager(sdk.HttpSource(base), sdk.UpdateOptions(
                False, config['maximum_deltas_before_fallback'], channel), **locator_args)
        except (ImportError, RuntimeError):
            return self.status()
        if manager.get_app_id() != config['package_id']:
            raise ValueError('更新包标识与当前应用不匹配')
        # Velopack labels all managed macOS apps portable; this is not an
        # installation-capability check. Successful locator/app ID is decisive.
        if Version(manager.get_current_version()) != Version(selected['current_version']):
            raise ValueError('安装版本与当前应用不匹配，未开始更新')
        if selected['status'] != 'available':
            self._state.update(mode='managed', channel=channel, message='应用内更新已启用；当前没有需要安装的新版本。')
            return self.status()
        feed = read_update_feed(base + f'releases.{channel}.json')
        assets = feed.get('Assets') if isinstance(feed, dict) else None
        if not isinstance(assets, list) or not assets:
            raise ValueError('更新频道内容为空或格式异常')
        approved = {}
        for asset in assets:
            self._validate_asset(asset, config['package_id'], channel, version)
            if asset['FileName'] in approved:
                raise ValueError('更新清单包含重复文件')
            approved[asset['FileName']] = asset
        info = manager.check_for_updates()
        if info is None:
            raise ValueError('发布页面与更新频道不一致，请稍后重试')
        if info.IsDowngrade:
            raise ValueError('不允许降级或切换频道')
        for asset in [info.TargetFullRelease, *info.DeltasToTarget]:
            actual = {name:getattr(asset, name) for name in ('PackageId','Version','Type','FileName','SHA256','Size')}
            self._validate_asset(actual, config['package_id'], channel, version)
            if any(approved.get(asset.FileName, {}).get(key) != value for key,value in actual.items()):
                raise ValueError('更新清单在检查期间发生变化，请重试')
        if info.TargetFullRelease.Type != 'Full' or info.TargetFullRelease.Version != version:
            raise ValueError('缺少完整更新包')
        if any(x.Type != 'Delta' or Version(x.Version) <= Version(manager.get_current_version()) for x in info.DeltasToTarget):
            raise ValueError('差分更新链版本无效')
        self._target_content_sha = approved[info.TargetFullRelease.FileName].get('ContentSHA256')
        if info.DeltasToTarget and not self._target_content_sha:
            # Legacy feeds cannot authenticate a recompressed ZIP's content.
            # Keep the same target but explicitly request the verified full.
            info.DeltasToTarget=[]
            info.BaseRelease=None
        self._manager, self._info = manager, info
        self._state = {**selected, 'mode':'managed', 'channel':channel, 'progress':0,
                       'size_bytes':info.TargetFullRelease.Size,
                       'download_size_bytes':sum(x.Size for x in info.DeltasToTarget) or info.TargetFullRelease.Size,
                       'strategy':'delta_with_full_fallback' if info.DeltasToTarget else 'full',
                       'message':'差分优先；SDK 校验或差分合成失败时可能额外下载完整包。' if info.DeltasToTarget else '此次使用完整更新包，由更新引擎校验后安装。'}
        return self.status()

    @staticmethod
    def _validate_asset(asset, package_id, channel, version):
        if not isinstance(asset, dict):
            raise ValueError('更新包元数据无效')
        name = asset.get('FileName', '')
        kind = asset.get('Type')
        asset_version = asset.get('Version', '')
        if 'ContentSHA256' in asset and not re.fullmatch(r'[A-Fa-f0-9]{64}', str(asset['ContentSHA256'])):
            raise ValueError('更新包内容摘要无效')
        if (asset.get('PackageId') != package_id
                or not re.fullmatch(r'\d+\.\d+\.\d+', str(asset_version))
                or Version(asset_version) > Version(version)
                or kind not in {'Full', 'Delta'}
                or not re.fullmatch(re.escape(package_id)+'-'+re.escape(asset_version)+'-'+re.escape(channel)+'-'+str(kind).lower()+r'\.nupkg', name)
                or not re.fullmatch(r'[A-Fa-f0-9]{64}', str(asset.get('SHA256', '')))
                or type(asset.get('Size')) is not int or asset['Size'] <= 0):
            raise ValueError('更新包的平台、版本、标识、大小或 SHA256 摘要无效')

    def download(self):
        with self._lock, self._runtime_admission.work():
            if self._state['status'] == 'downloading':
                return self.status()
            if not self._info or self._state['status'] not in {'available','download_failed'}:
                raise ValueError('请先检查可用的应用内更新；旧安装需使用完整安装包')
            self._state.update(status='downloading', progress=0)
            self._download_thread = threading.Thread(target=self._download, daemon=True, name='shaq-update-download')
            from .update_admission import start_guarded_thread
            try:
                start_guarded_thread(self.paths, self._download_thread)
            except Exception:
                self._state['status'] = 'available'
                raise
            return self.status()

    def _download(self):
        def progress(value):
            with self._lock:
                self._state['progress'] = max(0, min(100, int(value)))
        try:
            if getattr(self, '_native_cache', None):
                from .update_native import verify_cached
                verify_cached(self._native_cache, self._info.TargetFullRelease, required=False, content_sha256=getattr(self,'_target_content_sha',None))
            self._manager.download_updates(self._info, progress)
            if getattr(self, '_native_cache', None):
                verify_cached(self._native_cache, self._info.TargetFullRelease, content_sha256=getattr(self,'_target_content_sha',None))
            with self._lock:
                self._state.update(status='ready', progress=100,
                                   message='下载及校验完成；更新并重启前会检查所有分析、结算和后台写入。')
        except Exception:
            with self._lock:
                self._state.update(status='download_failed', message='下载或完整包校验失败，未安装更新；请检查网络后重试。')

    def apply(self, *, method='manual'):
        from .update_admission import gate_for, UpdateBusy, StaleRuntime
        with self._lock:
            if self._state['status'] != 'ready':
                raise ValueError('更新尚未下载并校验完成')
            gate = gate_for(self.paths)
            try:
                with gate.install():
                    self._runtime_admission.assert_current()
                    # Preference writes hold a work lease; admission therefore
                    # serializes this final check across processes as well.
                    # After mark_installing, preference writes cannot cancel it.
                    if method == 'automatic' and not self.status()['automatic_enabled']:
                        self._state.update(waiting_for_idle=False, queued_apply_method=None,
                                           queued_apply_target_version=None)
                        return self.status()
                    gate.mark_installing(self._state['latest_version'], method)
                    self._state.update(waiting_for_idle=False, waiting_for_edits=False, queued_apply_method=None,
                                       queued_apply_target_version=None, status='applying')
                    try:
                        if getattr(self, '_native_cache', None):
                            from .update_native import verify_cached
                            verify_cached(self._native_cache, self._info.TargetFullRelease, content_sha256=getattr(self,'_target_content_sha',None))
                        if getattr(self, 'gui_session', None):
                            self.gui_session.quiesce()
                        self._manager.apply_updates_and_restart(self._info)
                    except Exception as exc:
                        gate.cancel_failed_launch()
                        self._state.update(status='ready', message='未能启动安装；当前版本及资料保留，请重试。')
                        if getattr(self, 'gui_session', None):
                            self.gui_session.cancel()
                        from .update_gui import UnsavedEdits
                        if isinstance(exc, UnsavedEdits):
                            self._state.update(waiting_for_edits=True,message=str(exc))
                            if method=='automatic':return self.status()
                        raise
            except StaleRuntime:
                raise
            except UpdateBusy:
                self._state.update(waiting_for_idle=True, queued_apply_method=method,
                                   queued_apply_target_version=self._state['latest_version'],
                                   message='已下载，等待本地任务运行完更新')
            return self.status()

    def cancel_queued_apply(self):
        with self._lock:
            self._runtime_admission.assert_current()
            if self._state['status'] == 'applying':
                raise ValueError('安装已开始，不能取消')
            self._state.update(waiting_for_idle=False, queued_apply_method=None,
                               queued_apply_target_version=None, message='已取消本次等待；更新包保留，尚未安装。')
            return self.status()
