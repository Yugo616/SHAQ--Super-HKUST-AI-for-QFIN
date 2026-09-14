"""One Windows delivery path: collect every safe check, promote only complete acceptance."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
from types import SimpleNamespace


INSTALLER = 'SHAQ-Daily-Oracle-Lab-Windows-x64-Setup.exe'
APP = 'SHAQ Daily Oracle Lab'


def run_stage(name, args, cwd, reports, timeout, report=None, env=None):
    result = {'name': name, 'status': 'failed', 'timeout_seconds': timeout}
    try:
        if report is not None and report.exists():
            report.unlink()  # This stage owns this generated evidence; never accept a prior attempt.
        with (reports / (name + '.log')).open('w', encoding='utf-8') as log:
            process = subprocess.Popen([str(arg) for arg in args], cwd=cwd, stdout=log,
                                       stderr=subprocess.STDOUT, env=env)
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    if sys.platform == 'win32':
                        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                       stdout=log, stderr=subprocess.STDOUT, timeout=30, check=False)
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=15)
                raise
        result['exit_code'] = process.returncode
        if process.returncode:
            result['error'] = f'Process exited {process.returncode}'
        else:
            if report is not None:
                evidence = json.loads(report.read_text(encoding='utf-8'))
                if not isinstance(evidence, dict):
                    raise ValueError(f'Evidence must be a JSON object: {report.name}')
                if evidence.get('status') != 'passed':
                    raise ValueError(f'Unsuccessful evidence: {report.name}')
                if name.endswith('smoke'):
                    checks, methods = evidence.get('checks'), evidence.get('methods')
                    if (not isinstance(checks, dict) or not checks or not all(checks.values()) or
                            not isinstance(methods, list) or len(methods) != 2):
                        raise ValueError('Missing successful two-method smoke checks')
                if name.endswith('gui') and evidence.get('pages') != ['run', 'editor', 'history']:
                    raise ValueError('All three native pages must render')
                result['report'] = report.name
            result['status'] = 'passed'
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        result['error'] = str(exc)
    print(json.dumps(result), flush=True)
    return result


def find_inno():
    found = shutil.which('ISCC.exe')
    candidates = ([Path(found)] if found else []) + [Path(os.environ[key]) / 'Inno Setup 6/ISCC.exe'
                   for key in ('ProgramFiles(x86)', 'ProgramFiles', 'LOCALAPPDATA') if os.environ.get(key)]
    return next((path for path in candidates if path.is_file()), None)


def windows_prerequisites(compiler=None):
    import winreg
    key = r'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
    versions = []
    for hive, view in ((winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
                       (winreg.HKEY_CURRENT_USER, 0)):
        try:
            with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as handle:
                version = winreg.QueryValueEx(handle, 'pv')[0]
                if version and version != '0.0.0.0':
                    versions.append(version)
        except FileNotFoundError:
            pass
    compiler = compiler or find_inno()
    errors = [] if versions else ['WebView2 Evergreen Runtime is not registered']
    version = None
    if compiler and compiler.is_file():
        try:
            version = subprocess.check_output(['powershell.exe', '-NoProfile', '-Command',
                '[Diagnostics.FileVersionInfo]::GetVersionInfo($env:SHAQ_ISCC).FileVersion'],
                env=dict(os.environ, SHAQ_ISCC=str(compiler)), text=True, encoding='utf-8', timeout=15).strip()
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f'Cannot inspect Inno version: {exc}')
    else:
        errors.append('Inno Setup 6 ISCC.exe was not found')
    return {'status': 'failed' if errors else 'passed', 'webview2_versions': versions,
            'iscc_path': str(compiler) if compiler else None, 'iscc_version': version,
            'host': platform.platform(), 'architecture': platform.machine(), 'errors': errors}


def prepare_prerequisites(root, compiler):
    reports = root / 'dist/diagnostic'
    reports.mkdir(parents=True, exist_ok=True)
    before = windows_prerequisites(compiler)
    provisioning = []
    if not before['webview2_versions']:
        source = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703'
        bootstrap = reports / 'MicrosoftEdgeWebview2Setup.exe'
        try:
            with urllib.request.urlopen(source, timeout=60) as response, bootstrap.open('wb') as output:
                shutil.copyfileobj(response, output)
            # Verify the Authenticode trust chain and Microsoft publisher before execution.
            script = ('$s=Get-AuthenticodeSignature -LiteralPath $env:SHAQ_BOOTSTRAP; '
                      '$s | Select-Object Status,@{n="Publisher";e={$_.SignerCertificate.Subject}} | ConvertTo-Json; '
                      'if ($s.Status -ne "Valid" -or $s.SignerCertificate.Subject -notmatch "(^|, )CN=Microsoft Corporation(,|$)") {exit 2}')
            signature = run_stage('webview2-signature', ['pwsh', '-NoProfile', '-Command', script], root, reports, 30,
                                  env=dict(os.environ, SHAQ_BOOTSTRAP=str(bootstrap)))
            provisioning.append(signature)
            if signature['status'] == 'passed':
                provisioning.append(run_stage('webview2-install', [bootstrap, '/silent', '/install'], root, reports, 240))
        except (OSError, ValueError) as exc:
            provisioning.append({'name': 'webview2-download', 'status': 'failed', 'error': str(exc)})
    after = windows_prerequisites(compiler)
    result = {'status': 'passed' if after['status'] == 'passed' and all(x['status']=='passed' for x in provisioning) else 'failed',
              'before': before, 'after': after, 'provisioning': provisioning,
              'runtime_source': source if not before['webview2_versions'] else 'already installed'}
    (reports / 'prerequisites.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result), flush=True)
    return result


def validate_delivery(root, workspace, diagnostic, compiler, upstream_failed=False):
    workspace.mkdir(parents=True, exist_ok=True)
    reports = root / 'dist/diagnostic'
    reports.mkdir(parents=True, exist_ok=True)
    payload = workspace / 'payload' / APP
    executable = payload / (APP + '.exe')
    installer = workspace / 'installer' / INSTALLER
    installed = workspace / 'installed'
    # The Velopack root launcher detaches. Waiting for it can observe exit 0
    # before smoke evidence exists; wait for the actual installed process.
    installed_exe = installed / 'current' / (APP + '.exe')
    stages = [{'name': 'source-and-native-checks', 'status': 'failed' if upstream_failed else 'passed'}]
    try:
        stages.append({'name': 'prerequisites', **windows_prerequisites(compiler)})
    except OSError as exc:
        stages.append({'name': 'prerequisites', 'status': 'failed', 'error': str(exc)})

    def stage(name, args, timeout, report=None, requires=None):
        if requires is not None and not requires.is_file():
            result = {'name': name, 'status': 'blocked', 'error': f'Missing prerequisite: {requires.name}'}
            print(json.dumps(result), flush=True)
        else:
            result = run_stage(name, args, root, reports, timeout, report)
        stages.append(result)

    python = sys.executable
    version = os.environ.get('SHAQ_BUILD_VERSION') or tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    stage('build-app', [python, root / 'packaging/build_desktop.py', '--output', workspace / 'payload', '--version', version], 1200)
    stage('packaged-smoke', [executable, '--smoke', '--smoke-output', reports / 'smoke.json'],
          180, reports / 'smoke.json', executable)
    stage('payload-audit', [python, root / 'packaging/audit_payload.py', payload, '--output', reports / 'native-audit.json'],
          180, reports / 'native-audit.json', executable)
    stage('compile-installer', [python, root / 'packaging/build_desktop.py', '--manage-existing', payload,
          '--version', version, '--output', installer.parent, '--prepare-public-base'],
          600, requires=executable)
    try:
        stage('install', [installer, '--silent', '--installto', installed,
              '--log', reports / 'install-velopack.log'], 180, requires=installer)
        stage('installed-smoke', [installed_exe, '--smoke', '--smoke-output', reports / 'installed-smoke.json'],
              180, reports / 'installed-smoke.json', installed_exe)
        stage('installed-gui', [installed_exe, '--gui-smoke', reports / 'installed-gui.json'],
              75, reports / 'installed-gui.json', installed_exe)
        stage('installed-audit', [python, root / 'packaging/audit_payload.py', installed,
              '--output', reports / 'installed-native-audit.json'], 180, reports / 'installed-native-audit.json', installed_exe)
    finally:
        uninstaller = installed / 'Update.exe'
        stage('uninstall', [uninstaller, 'uninstall', '--silent',
              '--log', reports / 'uninstall-velopack.log'], 120, requires=uninstaller)
        stage('uninstall-check', [python, root / 'packaging/verify_uninstall.py', installed,
              '--timeout-seconds', '30', '--output', reports / 'uninstall-report.json'],
              45, reports / 'uninstall-report.json')
    passed = all(item['status'] == 'passed' for item in stages)
    # A diagnostic installer is evidence only, even when all checks happen to pass.
    if installer.is_file():
        shutil.copy2(installer, reports / INSTALLER)
    if passed and not diagnostic:
        try:
            promote_final_delivery(root, installer, version)
        except (OSError, ValueError) as exc:
            passed = False
            stages.append({'name': 'release-feed-promotion', 'status': 'failed', 'error': str(exc)})
    result = {'status': 'passed' if passed else 'failed', 'mode': 'diagnostic' if diagnostic else 'final',
              'release_promoted': passed and not diagnostic, 'stages': stages}
    (reports / 'windows-delivery.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    return result


def promote_final_delivery(root, installer, version):
    """Copy only this accepted installer's validated feed, never acceptance fixtures."""
    from shaq_daily_oracle.software_updates import UpdateRuntime
    from shaq_daily_oracle.update_native import verify_cached
    updates = json.loads((root / 'config/software-updates.json').read_text())
    tools = json.loads((root / 'packaging/updater-toolchain.json').read_text())
    channel = updates['channels']['Windows/amd64']
    feed_name = 'releases.' + channel + '.json'
    receipt_name = 'delta-base.' + channel + '.json'
    feed_root = installer.parent
    receipt = json.loads((feed_root / receipt_name).read_text())
    if (receipt.get('target_version') != version or receipt.get('channel') != channel or
            receipt.get('status') not in {'public-base', 'first-managed-release'}):
        raise ValueError('Final feed base receipt does not match the accepted build')
    feed = json.loads((feed_root / feed_name).read_text())
    assets = feed.get('Assets') if isinstance(feed, dict) else None
    if not isinstance(assets, list) or not assets:
        raise ValueError('Missing final release feed')
    names = set()
    target_full = []
    for asset in assets:
        UpdateRuntime._validate_asset(asset, updates['package_id'], channel, version)
        if asset['Version'] in {tools['acceptance_prior_version'], tools['acceptance_bridge_version']} or asset['FileName'] in names:
            raise ValueError('Final feed contains internal acceptance or duplicate packages')
        verify_cached(feed_root, SimpleNamespace(**asset))
        names.add(asset['FileName'])
        if asset['Type'] == 'Full' and asset['Version'] == version:
            target_full.append(asset)
    if len(target_full) != 1:
        raise ValueError('Final feed does not contain the accepted target full package')
    destination = root / 'dist/update-feed'
    if destination.exists() or destination.is_symlink():
        raise ValueError('Final release feed destination must be fresh')
    target = root / 'dist' / INSTALLER
    with tempfile.TemporaryDirectory(prefix='shaq-final-feed-', dir=target.parent) as directory:
        staged = Path(directory)
        for name in sorted(names | {feed_name, receipt_name}):
            shutil.copy2(feed_root / name, staged / name)
        digest = hashlib.sha256(installer.read_bytes()).hexdigest()
        (staged / 'delivery.json').write_text(json.dumps(dict(target_version=version, channel=channel,
            installer=INSTALLER, installer_sha256=digest), indent=2), encoding='utf-8')
        shutil.copy2(installer, target)
        target.with_name(target.name + '.sha256').write_text(f'{digest}  {target.name}\n', encoding='utf-8')
        staged.replace(destination)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--diagnostic', action='store_true')
    parser.add_argument('--preflight-only', action='store_true')
    parser.add_argument('--upstream-failed', action='store_true')
    args = parser.parse_args()
    if (sys.platform != 'win32' or os.environ.get('GITHUB_ACTIONS') != 'true' or
            os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted'):
        parser.error('Installation acceptance requires an isolated GitHub-hosted Windows runner')
    root = Path(__file__).resolve().parents[1]
    compiler = find_inno() or Path('ISCC.exe')
    if args.preflight_only:
        result = prepare_prerequisites(root, compiler)
    else:
        workspace = Path(tempfile.mkdtemp(prefix='shaq-windows-delivery-', dir=os.environ['RUNNER_TEMP']))
        result = validate_delivery(root, workspace, args.diagnostic, compiler, args.upstream_failed)
    print(json.dumps(result), flush=True)
    return 0 if result['status'] == 'passed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
