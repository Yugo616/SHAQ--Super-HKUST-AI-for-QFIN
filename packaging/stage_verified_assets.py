"""Assemble public assets only after downloaded native acceptance passes."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import tarfile


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def passed(path):
    data = read(path)
    if data.get('status') != 'passed':
        raise ValueError(f'Acceptance failed: {path.name}')
    return data


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def windows_clean_gates(source):
    """Older Windows receipts record clean checks in CI steps, not JSON flags."""
    workflow = source.get('validation_workflow_content', '')
    workflow_hash = source.get('validation_files_sha256', {}).get('.github/workflows/build-desktop.yml')
    if not workflow or hashlib.sha256(workflow.encode()).hexdigest() != workflow_hash:
        raise ValueError('External layout: independently bound workflow is required')
    gates = (
        ('Verify frozen application checkout and record both identities',
         "if ($applicationSha -ne $env:APPLICATION_REF -or (git status --porcelain)) { throw 'Application must be the exact clean requested commit' }"),
        ('Full installed frozen-app acceptance with bounded pre-apply setup recovery',
         "if ((git rev-parse HEAD).Trim() -ne $env:SHAQ_LAYOUT_APPLICATION_SHA -or (git status --porcelain)) { throw 'Installed validation changed frozen application source' }"),
    )
    lines = workflow.splitlines()
    for name, guard in gates:
        runs = [step for step in source['job'].get('steps', []) if step.get('name') == name]
        starts = [i for i, line in enumerate(lines) if line.strip() == '- name: ' + name]
        if len(runs) != 1 or runs[0].get('status') != 'completed' or runs[0].get('conclusion') != 'success' or len(starts) != 1:
            raise ValueError('External layout: required clean gate did not pass')
        start = starts[0]
        indent = len(lines[start]) - len(lines[start].lstrip())
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].strip().startswith('- ') and len(lines[i]) - len(lines[i].lstrip()) <= indent), len(lines))
        if guard not in [line.strip() for line in lines[start + 1:end]]:
            raise ValueError('External layout: workflow clean guard differs')
    return dict(workflow_sha256=workflow_hash, before_step=gates[0][0], after_step=gates[1][0])


def native_identity(source, commit, platform, job_url, layout, external):
    if source is None:
        if external is not None or layout.get('validation_sha'):
            raise ValueError('External layout: independent native source metadata is required')
        return None, None
    run, job, artifact = (source.get(name, {}) for name in ('run', 'job', 'artifact'))
    repository = source.get('repository', '')
    origin, repo = artifact.get('workflow_run', {}), run.get('repository', {})
    head = run.get('head_sha', '')
    if (not re.fullmatch(r'[\w.-]+/[\w.-]+', repository) or
            not re.fullmatch('[a-f0-9]{40}', head) or
            any(type(row.get('id')) is not int or row['id'] <= 0 for row in (run, job, artifact)) or
            run.get('path') != '.github/workflows/build-desktop.yml' or
            run.get('status') != 'completed' or not run.get('conclusion') or
            repo.get('full_name') != repository or not repo.get('id') or
            job.get('run_id') != run['id'] or job.get('status') != 'completed' or
            job.get('conclusion') != 'success' or platform not in job.get('name', '') or
            origin.get('id') != run['id'] or origin.get('head_sha') != head or
            origin.get('repository_id') != repo['id'] or origin.get('head_repository_id') != repo['id'] or
            artifact.get('expired') is not False or
            artifact.get('name') != 'SHAQ-Daily-Oracle-Lab-' + platform or
            not re.fullmatch('sha256:[a-f0-9]{64}', artifact.get('digest', '')) or
            job_url != f'https://github.com/{repository}/actions/runs/{run["id"]}/job/{job["id"]}'):
        raise ValueError('Native source: source run/job/artifact identity mismatch')
    binding = dict(repository=repository, application_sha=commit, platform=platform,
        run_id=run['id'], run_head_sha=head, run_conclusion=run['conclusion'],
        job_id=job['id'], job_name=job['name'], job_conclusion=job['conclusion'],
        artifact_id=artifact['id'], artifact_name=artifact['name'], artifact_digest=artifact['digest'])
    if external is None:
        if head != commit or layout.get('application_sha') or layout.get('validation_sha'):
            raise ValueError('External layout: source metadata is missing for the validation branch')
        return binding, None
    external = dict(external)
    clean_evidence = None
    if platform == 'Windows-x64' and not any(key in external for key in ('application_clean_before', 'application_clean_after')):
        clean_evidence = windows_clean_gates(source)
        external.update(application_clean_before=True, application_clean_after=True)
    expected = dict(application_sha=commit, validation_sha=head,
                    application_clean_before=True, application_clean_after=True)
    if (any(external.get(key) != value for key, value in expected.items()) or
            any(external.get(key) is not True for key in ('application_clean_before', 'application_clean_after')) or
            layout.get('application_sha') != commit or layout.get('validation_sha') != head):
        raise ValueError('External layout: application, validation or clean-check identity mismatch')
    hashes = source.get('validation_files_sha256', {})
    for field, path in (('layout_sha256', 'tests/native_layout_check.py'),
                        ('geometry_sha256', 'tests/native_geometry.py')):
        actual = hashes.get(path, '')
        if not re.fullmatch('[a-f0-9]{64}', actual) or str(external.get(field, '')).lower() != actual:
            raise ValueError('External layout: harness hash differs from the actual run commit')
        expected[field] = actual
    expected['source'] = 'GitHub commit content verified independently'
    if clean_evidence is not None:
        expected['clean_check_evidence'] = clean_evidence
    return binding, expected


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--artifact', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--platform', required=True)
    p.add_argument('--version', required=True)
    p.add_argument('--commit', required=True)
    p.add_argument('--job-url', required=True)
    p.add_argument('--native-public-recheck', type=Path)
    p.add_argument('--native-source', type=Path)
    a = p.parse_args()
    root = a.artifact
    dist = root / 'dist'
    meta = read(root / 'build/third-party/manifest.json')
    if meta['source_sha'] != a.commit or meta['source_dirty'] or meta['source_version'] != a.version:
        raise ValueError('Wrong source identity')
    layout = passed(dist / 'diagnostic/desktop-layout.json')
    external_layout = dist / 'diagnostic/external-layout-source.json'
    native_source, harness = native_identity(
        read(a.native_source) if a.native_source else None, a.commit, a.platform, a.job_url,
        layout, read(external_layout) if external_layout.exists() else None)
    transition = passed(dist / 'installed-update/acceptance.json')
    windows = a.platform == 'Windows-x64'
    reports = dist / 'diagnostic' if windows else dist
    if windows:
        delivery = passed(reports / 'windows-delivery.json')
        if not delivery['release_promoted'] or any(s['status'] != 'passed' for s in delivery['stages']):
            raise ValueError('Windows final delivery incomplete')
    gui = passed(reports / 'installed-gui.json')
    passed(reports / 'installed-smoke.json')
    passed(reports / 'installed-native-audit.json')
    passed(reports / 'installed-worker-protocol.json')
    original_public = read(reports / 'public-base-update.json')
    public = passed(a.native_public_recheck or (reports / 'public-base-update.json'))
    identity = public['identity']
    if identity['target_version'] != a.version or public['target_full_requests'] or not public['uninstalled']:
        raise ValueError('Public-base delta upgrade not established')
    channel = identity['channel']
    out = a.output
    out.mkdir(parents=True, exist_ok=True)
    installer_name = ('SHAQ-Daily-Oracle-Lab-Windows-x64-Setup.exe' if windows
                      else f'SHAQ-Daily-Oracle-Lab-{a.platform}.dmg')
    installer = dist / installer_name
    expected = (dist / (installer_name + '.sha256')).read_text().split()[0].lower()
    if digest(installer) != expected:
        raise ValueError('Installer checksum mismatch')
    shutil.copy2(installer, out / installer.name)
    feed_root = dist / 'update-feed'
    feed_name = f'releases.{channel}.json'
    feed = read(feed_root / feed_name)
    target_full = next(asset for asset in feed['Assets']
                       if asset['FileName'] == identity['target_filename'])
    if target_full['SHA256'].lower() != identity['target_sha256'].lower():
        raise ValueError('Native recheck used different application package')
    for asset in feed['Assets']:
        name = asset['FileName']
        if Path(name).name != name:
            raise ValueError('Invalid asset name')
        path = feed_root / name
        if path.stat().st_size != asset['Size'] or digest(path) != asset['SHA256'].lower():
            raise ValueError('Feed asset checksum mismatch')
        shutil.copy2(path, out / name)
    for name in (feed_name, f'delta-base.{channel}.json'):
        shutil.copy2(feed_root / name, out / name)
    archive = out / f'ThirdParty-{a.platform}.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        for relative in ('build/third-party', 'build/license-sources', 'build/native-dependencies/sources'):
            path = root / relative
            if path.exists():
                tar.add(path, arcname=relative.removeprefix('build/'))
    delta = feed_root / identity['delta_filename']
    receipt = dict(application_version=a.version, application_commit=a.commit, platform=a.platform,
        native_ci=a.job_url, status='passed', skill_validation={'passed':8},
        native_layout={'status':layout['status'], 'checks':len(layout.get('checks',[])),
                       'geometry_cases':len(layout.get('geometry',[]))},
        installed_application={'startup':'passed', 'shared_evidence_two_methods':'passed with deterministic model fixture',
            'history_and_account_reopen':'passed', 'gui_checks':gui, 'privacy_audit':'passed', 'uninstall':'passed'},
        actual_public_base_delta={'base_version':identity['base_version'], 'target_version':a.version,
            'status':'passed', 'downloaded_target_full_package':False,
            'acceptance_host':'local native Mac' if a.native_public_recheck else 'native CI',
            'ci_public_base_status':original_public['status'],
            'ci_public_base_error':original_public.get('error'),
            'preserved_fixture_data_hashes':public['target']['preserved_hashes'],
            'target_reopen':public['replay']['status'], 'delta_bytes':delta.stat().st_size},
        limitations=['Native CI does not certify every personal Windows or Mac installation.',
            'Model calls use deterministic substitutes, not personal credentials.',
            'Live provider entitlement, regional connectivity and predictive performance are not certified.'])
    if native_source is not None:
        receipt['native_source'] = native_source
    if harness is not None:
        receipt['external_layout_harness'] = harness
    external = reports / 'validation-tests.json'
    if external.exists():
        validation = passed(external)
        if validation['application_sha'] != a.commit or validation['exit_code'] != 0:
            raise ValueError('External validation application identity mismatch')
        receipt['reviewed_test_harness'] = {
            'validation_commit': validation['validation_sha'],
            'application_commit': validation['application_sha'],
            'runtime_files_verified_identical': len(validation['runtime_files_sha256']),
            'status': validation['status'],
            'scope': 'Test fixture wait and owned worker cleanup; application runtime unchanged',
        }
    # GUI receipt contains only booleans, page names and fixture ticker strings.
    serialized = json.dumps(receipt, ensure_ascii=False, indent=2)
    if '/Users/' in serialized or ':\\Users\\' in serialized:
        raise ValueError('Private path in public receipt')
    (out / f'Acceptance-{a.platform}.json').write_text(serialized + '\n', encoding='utf-8')
    files = sorted(f for f in out.iterdir() if f.is_file() and not f.name.startswith('SHA256SUMS'))
    (out / f'SHA256SUMS-{a.platform}.txt').write_text(
        ''.join(f'{digest(f)}  {f.name}\n' for f in files), encoding='utf-8')
    print(json.dumps({'platform':a.platform,'status':'verified','files':len(files),
                      'delta_bytes':delta.stat().st_size,'source':a.commit}))


if __name__ == '__main__':
    main()

