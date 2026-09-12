"""Read-only platform release discovery. No downloads, executable replacement or credentials."""
import json
import platform
import re
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
