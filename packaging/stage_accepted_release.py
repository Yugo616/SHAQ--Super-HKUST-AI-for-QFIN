"""Stage only accepted platform release files, never local research records."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tarfile

VERSION = os.environ['SHAQ_RELEASE_VERSION']
SHA = os.environ['SHAQ_APPLICATION_SHA']
REPO = 'Yugo616/SHAQ--Super-HKUST-AI-for-QFIN'

def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))

def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def require(value, explanation):
    if not value:
        raise ValueError(explanation)

def stage(root, target, platform, jobs):
    windows = platform == 'Windows-x64'
    diagnostics = root/'dist'/'diagnostic'
    installed = diagnostics if windows else root/'dist'
    manifest = read(root/'build/third-party/manifest.json')
    require(manifest['source_sha'] == SHA and manifest['source_dirty'] is False,
            'Wrong or dirty source')
    local = Path(os.environ['SHAQ_APPLICATION_ROOT'])/'bundled_versions'
    methods = {p.relative_to(local).as_posix(): digest(p) for p in local.rglob('*') if p.is_file()}
    require({k.replace('\\','/'):v for k,v in manifest['methods'].items()} == methods,
            'Method package mismatch')
    job = next(j for j in jobs['jobs'] if platform in j['name'] and j['conclusion'] == 'success')
    required_steps = ['Validate source privacy and eight Skills before building',
                      'Retain installers, checksums, provenance, native sources and acceptance evidence']
    for name in required_steps:
        require(any(s['name'] == name and s['conclusion'] == 'success' for s in job['steps']), name)
    layout = read(diagnostics/'desktop-layout.json')
    require(layout['status'] == 'passed' and not layout.get('failures'), 'Layout failed')
    require(layout.get('progress_preserved_selection_and_expansion') is True and
            layout.get('pending_refresh_preserved_latest_scroll') is True, 'Refresh persistence failed')
    require(layout.get('resume_progress_visible') is True, 'Recovery progress not visible')
    require(layout.get('external_job_discovery') is True and
            layout.get('backend_total_progress') is True, 'External job discovery or total progress failed')
    acceptance = read(root/'dist/installed-update/acceptance.json')
    require(acceptance['status'] == 'passed', 'Managed installation failed')
    for filename in ['installed-smoke.json', 'installed-gui.json', 'installed-native-audit.json',
                     'installed-worker-protocol.json']:
        require(read(installed/filename)['status'] == 'passed', filename)
    smoke = read(installed/'installed-smoke.json')
    require(smoke['provider_secrets_used'] is False and smoke['broker_modules_loaded'] is False and
            smoke['reopen_matches'] is True and all(smoke['checks'].values()), 'Smoke contract failed')
    public = read(installed/'public-base-update.json')
    identity = public['identity']
    require(public['status'] == 'passed' and identity['target_version'] == VERSION and
            identity['base_version'] == '0.7.7' and public['target_full_requests'] == [] and
            public['target']['preserved_hashes'] is True and public['replay']['status'] == 'passed',
            'Actual public-base delta was not accepted')
    channel = identity['channel']
    feed_root = root/'dist/update-feed'
    feed = read(feed_root/f'releases.{channel}.json')
    target.mkdir(parents=True, exist_ok=True)
    def copy(source):
        destination = target/source.name
        if destination.exists():
            require(digest(destination) == digest(source), 'Conflicting staged release asset')
        else:
            shutil.copy2(source, destination)
    for asset in feed['Assets']:
        name = asset['FileName']
        require(Path(name).name == name and asset['Version'] in {'0.7.7', VERSION}, 'Unsafe feed asset')
        path = feed_root/name
        require(path.stat().st_size == asset['Size'] and digest(path) == asset['SHA256'].lower(),
                'Feed asset digest mismatch')
        copy(path)
    copy(feed_root/f'releases.{channel}.json')
    legacy_feed = feed_root/f'RELEASES-{channel}'
    if legacy_feed.exists(): copy(legacy_feed)
    installer = root/'dist'/('SHAQ-Daily-Oracle-Lab-Windows-x64-Setup.exe' if windows else
                            f'SHAQ-Daily-Oracle-Lab-{platform}.dmg')
    checksum = installer.with_name(installer.name+'.sha256')
    require(checksum.read_text().split()[0].lower() == digest(installer), 'Installer checksum mismatch')
    copy(installer)
    if windows:
        delivery = read(diagnostics/'windows-delivery.json')
        require(delivery['status'] == 'passed' and all(s['status'] == 'passed' for s in delivery['stages']),
                'Windows delivery did not pass all stages')
        external = read(diagnostics/'validation-tests.json')
        source = read(diagnostics/'external-layout-source.json')
        require(external['status'] == 'passed' and external['application_sha'] == SHA and
                external['validation_sha'] == source['validation_sha'] and source['application_sha'] == SHA,
                'External tests did not validate exact application')
    receipt = dict(application_version=VERSION, application_commit=SHA, platform=platform,
                   native_ci=job['html_url'], status='passed', skill_validation={'passed':8},
                   native_layout=dict(status='passed', measurements=len(layout['checks']),
                                      scroll_and_expansion_preserved=True),
                   installed_application=dict(startup='passed',
                     shared_evidence_two_methods='passed with deterministic model fixture',
                     history_and_account_reopen='passed', privacy_audit='passed', uninstall='passed'),
                   actual_public_base_delta=dict(base_version=identity['base_version'],target_version=VERSION,
                     status='passed',downloaded_target_full_package=False,preserved_fixture_data_hashes=True,
                     target_reopen='passed',delta_bytes=(feed_root/identity['delta_filename']).stat().st_size),
                   limitations=['Native CI validates this architecture, not every personal computer.',
                     'Model calls use deterministic substitutes, not personal model accounts.',
                     'No personal model login, relay access or regional network availability is certified.'])
    if windows:
        receipt['external_test_validation'] = dict(application_commit=SHA,
            validation_commit=external['validation_sha'], status='passed',
            runtime_files_verified_identical=True)
    (target/f'Acceptance-{platform}.json').write_text(json.dumps(receipt,indent=2)+'\n')
    with tarfile.open(target/f'ThirdParty-{platform}.tar.gz','w:gz') as archive:
        for relative in ['build/third-party','build/license-sources','build/native-dependencies/sources']:
            archive.add(root/relative, arcname=relative.removeprefix('build/'))
    sums = ''.join(f'{digest(p)}  {p.name}\n' for p in sorted(target.iterdir())
                   if p.is_file() and p.name != 'SHA256SUMS.txt')
    (target/'SHA256SUMS.txt').write_text(sums)
    print(json.dumps({'platform':platform,'status':'staged','files':len(list(target.iterdir())),
                      'delta_bytes':receipt['actual_public_base_delta']['delta_bytes']}))

if __name__ == '__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--artifact',type=Path,required=True)
    p.add_argument('--destination',type=Path,required=True)
    p.add_argument('--platform',required=True)
    p.add_argument('--jobs',type=Path,required=True)
    a=p.parse_args()
    stage(a.artifact,a.destination,a.platform,read(a.jobs))
