"""Install the verified public Windows base, then apply the final native delta."""
import argparse
import functools
import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import urllib.parse

import httpx
from packaging.version import Version


INSTALLER = 'SHAQ-Daily-Oracle-Lab-Windows-x64-Setup.exe'
APP = 'SHAQ Daily Oracle Lab'
LOOPBACK_STAGES = frozenset({'public-base-apply', 'public-base-replay'})


class DeltaOnlyHandler(http.server.SimpleHTTPRequestHandler):
    """Serve the final feed/delta while making any target-full fallback fatal."""
    def __init__(self, *args, blocked_filename, blocked_requests, **kwargs):
        self.blocked_filename = blocked_filename
        self.blocked_requests = blocked_requests
        super().__init__(*args, **kwargs)

    def do_GET(self):
        requested = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path).lstrip('/')
        if requested == self.blocked_filename:
            self.blocked_requests.append(requested)
            self.send_error(409, 'Target full fallback is forbidden in delta acceptance')
            return
        super().do_GET()


def create_acceptance_root():
    # Released 0.7.0 accepts only a direct child of tempfile.gettempdir().
    return Path(tempfile.mkdtemp(prefix='shaq-installed-update-')).resolve()


def run_child(command, stream, timeout, *, loopback=False):
    environment = dict(os.environ)
    if loopback:
        for key in ('NO_PROXY', 'no_proxy'):
            environment[key] = ','.join(filter(None, (
                environment.get(key, ''), '127.0.0.1', 'localhost')))
    return subprocess.run([str(value) for value in command], stdout=stream,
                          stderr=subprocess.STDOUT, timeout=timeout, env=environment)


def validate_transition(root, feed_root, target_version):
    """Bind public base, final full, and actual delta to one verified identity."""
    from shaq_daily_oracle.software_updates import UpdateRuntime
    from shaq_daily_oracle.update_native import verify_cached
    feed_root = Path(feed_root)
    updates = json.loads((root / 'config/software-updates.json').read_text(encoding='utf-8'))
    channel = updates['channels']['Windows/amd64']
    receipt = json.loads((feed_root / f'delta-base.{channel}.json').read_text(encoding='utf-8'))
    if (receipt.get('status') != 'public-base' or receipt.get('channel') != channel or
            receipt.get('target_version') != target_version or not receipt.get('base_version')):
        raise ValueError('Final Windows partial-update acceptance requires an exact public base receipt')
    if Version(receipt['base_version']) >= Version(target_version):
        raise ValueError('Public base must precede the final target')
    installer_url = (f'https://github.com/{receipt.get("repository", "")}/releases/download/'
                     f'lab-v{receipt["base_version"]}-windows/{INSTALLER}')
    if (receipt.get('installer_source_url') != installer_url or
            not isinstance(receipt.get('installer_size'), int) or receipt['installer_size'] <= 0 or
            not re.fullmatch(r'[A-Fa-f0-9]{64}', str(receipt.get('installer_sha256', '')))):
        raise ValueError('Public base installer identity is missing or invalid')
    feed = json.loads((feed_root / f'releases.{channel}.json').read_text(encoding='utf-8'))
    assets = feed.get('Assets') if isinstance(feed, dict) else None
    if not isinstance(assets, list):
        raise ValueError('Final Windows feed is invalid')
    for asset in assets:
        UpdateRuntime._validate_asset(asset, updates['package_id'], channel, target_version)
        verify_cached(feed_root, SimpleNamespace(**asset))
    base = [asset for asset in assets if asset['Version'] == receipt['base_version'] and asset['Type'] == 'Full']
    full = [asset for asset in assets if asset['Version'] == target_version and asset['Type'] == 'Full']
    delta = [asset for asset in assets if asset['Version'] == target_version and asset['Type'] == 'Delta']
    if (len(base) != 1 or base[0]['FileName'] != receipt.get('filename') or
            base[0]['SHA256'].lower() != str(receipt.get('sha256', '')).lower() or
            base[0]['Size'] != receipt.get('size')):
        raise ValueError('Final feed public base does not match its verified receipt')
    if len(full) != 1:
        raise ValueError('Final feed has no unambiguous target full package')
    if len(delta) != 1:
        raise ValueError('Final feed has no unambiguous actual target delta')
    return dict(base_version=receipt['base_version'], target_version=target_version,
                base_filename=base[0]['FileName'], base_sha256=base[0]['SHA256'].lower(),
                target_filename=full[0]['FileName'], target_sha256=full[0]['SHA256'].lower(),
                delta_filename=delta[0]['FileName'], delta_sha256=delta[0]['SHA256'].lower(),
                installer_source_url=receipt['installer_source_url'],
                installer_sha256=receipt['installer_sha256'].lower(), installer_size=receipt['installer_size'],
                channel=channel, package_id=updates['package_id'])


def download_verified(url, destination, size, digest):
    with httpx.Client(timeout=120, follow_redirects=True) as client, client.stream('GET', url) as response:
        response.raise_for_status()
        written = 0
        with destination.open('xb') as stream:
            for chunk in response.iter_bytes():
                written += len(chunk)
                if written > size:
                    raise ValueError('Public base installer exceeds its published size')
                stream.write(chunk)
    with destination.open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    if written != size or actual.lower() != digest.lower():
        raise ValueError('Public base installer failed published size or SHA256 verification')


def validate_update_events(identity, bridge, target, peer, replay):
    """Require the real bridge selection and each restarted-process identity."""
    if (bridge.get('status') != 'applying' or bridge.get('stage') != 'bridge' or
            bridge.get('actual_version') != identity['base_version'] or
            bridge.get('native_target') != identity['target_version'] or
            bridge.get('download_verified') is not True or bridge.get('preserved_hashes') is not True or
            not isinstance(bridge.get('delta_count'), int) or bridge['delta_count'] < 1):
        raise ValueError('Public base bridge did not verify and select the actual final delta')
    if (target.get('status') != 'passed' or target.get('stage') != 'target' or
            target.get('actual_version') != identity['target_version'] or
            target.get('preserved_hashes') is not True or target.get('remained_open_after_health') is not True or
            any(item.get('running') is not False for item in target.get('old_processes_before_data_access', [])) or
            len(target.get('old_processes_before_data_access', [])) != 2):
        raise ValueError('Updated target process identity or preservation evidence is invalid')
    if (peer.get('status') != 'passed' or peer.get('stage') != 'peer' or
            peer.get('actual_version') != identity['base_version'] or
            peer.get('preserved_hashes') is not True or peer.get('cooperatively_closed') is not True):
        raise ValueError('Public base peer process did not close cooperatively')
    if (replay.get('status') != 'passed' or replay.get('stage') != 'replay' or
            replay.get('actual_version') != identity['target_version'] or
            replay.get('preserved_hashes') is not True):
        raise ValueError('Updated public installation did not replay at the final version')
    return {'delta_count': bridge['delta_count'], 'base_version': identity['base_version'],
            'target_version': identity['target_version']}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--feed', type=Path, required=True)
    parser.add_argument('--target-version', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    project = Path(__file__).resolve().parents[1]
    stages = []
    result = {'status': 'failed', 'stages': stages}
    root = None
    server = None

    def run(name, command, timeout=300):
        log = args.output.with_name(args.output.stem + '-' + name + '.log')
        with log.open('w', encoding='utf-8') as stream:
            completed = run_child(command, stream, timeout, loopback=name in LOOPBACK_STAGES)
        stages.append({'name': name, 'returncode': completed.returncode})
        if completed.returncode:
            raise RuntimeError(name + ' failed; see retained log')

    try:
        if sys.platform != 'win32':
            raise RuntimeError('Public base native acceptance requires Windows')
        identity = validate_transition(project, args.feed.resolve(), args.target_version)
        root = create_acceptance_root()
        installed = root / 'installed' / APP
        setup = root / INSTALLER
        download_verified(identity['installer_source_url'], setup, identity['installer_size'], identity['installer_sha256'])
        run('public-base-install', [setup, '--silent', '--installto', installed])
        executable = installed / 'current' / (APP + '.exe')
        if not executable.is_file():
            raise RuntimeError('Public base installer did not create the native executable')
        cache = root / 'packages'
        cache.mkdir()
        shutil.copy2(args.feed / identity['base_filename'], cache / identity['base_filename'])
        blocked_requests = []
        handler = functools.partial(DeltaOnlyHandler, directory=str(args.feed.resolve()),
                                    blocked_filename=identity['target_filename'],
                                    blocked_requests=blocked_requests)
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        config = dict(bridge_version=identity['base_version'], target_version=identity['target_version'],
                      package_id=identity['package_id'], channel=identity['channel'],
                      feed_url=f'http://127.0.0.1:{server.server_port}/', events_directory='events-public-base')
        configuration = root / 'acceptance.json'
        configuration.write_text(json.dumps(config), encoding='utf-8')
        events = root / config['events_directory']
        run('public-base-apply', [executable, '--update-smoke', configuration], timeout=360)
        from shaq_daily_oracle.update_smoke import process_running, wait_event
        from installed_update_acceptance import uninstall_windows, verify_cache, verify_program_copies
        bridge = wait_event(events / 'bridge-result.json')
        health = wait_event(events / 'target-health.json', timeout=180, failure_path=events / 'target-result.json')
        if health.get('version') != identity['target_version'] or not process_running(health['pid']):
            raise RuntimeError('Updated target did not remain healthy at the final version')
        from shaq_daily_oracle.settings import _atomic_json
        _atomic_json(events / 'close-target.json', {'status': 'passed'})
        target = wait_event(events / 'target-result.json', timeout=60)
        deadline = time.monotonic() + 30
        while process_running(health['pid']) and time.monotonic() < deadline:
            time.sleep(.1)
        if process_running(health['pid']):
            raise RuntimeError('Updated target did not close after health confirmation')
        peer = wait_event(events / 'peer-result.json')
        if blocked_requests:
            raise RuntimeError('Native updater requested the target full fallback during delta acceptance')
        cache_files = verify_cache(cache, identity['target_filename'])
        program_copies = verify_program_copies(installed, sys.platform)
        run('public-base-replay', [executable, '--update-smoke', configuration, '--update-stage', 'replay'], timeout=120)
        replay = wait_event(events / 'replay-result.json')
        event_identity = validate_update_events(identity, bridge, target, peer, replay)
        uninstall_output = args.output.parent / 'public-base-update'
        uninstall_output.mkdir(exist_ok=True)
        uninstall_windows(run, installed, project, uninstall_output)
        if installed.exists():
            raise RuntimeError('Public base acceptance left the isolated installation')
        result = dict(status='passed', identity=identity, event_identity=event_identity,
                      bridge=bridge, target=target, peer=peer, replay=replay,
                      cache_files=cache_files, program_copies=program_copies, uninstalled=True,
                      target_full_requests=blocked_requests,
                      stages=stages, limitations=['Windows GitHub-hosted runner only',
                      'one public-base-to-final transition; internal multi-version soak remains separate'])
    except Exception as exc:
        result['error'] = str(exc)
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result), flush=True)
    return 0 if result['status'] == 'passed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
