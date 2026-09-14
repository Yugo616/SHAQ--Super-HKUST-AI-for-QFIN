"""One evidence-gated retry of initial installed fixture admission, never apply."""
import hashlib
import json
from pathlib import Path
import re
import time


def file_hashes(root):
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink(): raise RuntimeError('Recovery snapshot refuses symlinks')
        if path.is_file():
            with path.open('rb') as stream:
                result[path.relative_to(root).as_posix()] = hashlib.file_digest(stream, 'sha256').hexdigest()
    return result


def history_hashes(root):
    path = root/'data/software-update-history.json'
    return {path.name:hashlib.sha256(path.read_bytes()).hexdigest()} if path.exists() else {}


def probe_admission(root, *, timeout=5):
    from shaq_daily_oracle.update_admission import AdmissionGate, UpdateBusy
    gate = AdmissionGate(root/'data')
    deadline = time.monotonic()+timeout
    while True:
        try:
            # Probe the real gate and work leases; never mark or perform install.
            with gate.install(): pass
            with gate.work(): pass
            return
        except UpdateBusy:
            if time.monotonic() >= deadline: raise TimeoutError('Admission did not recover within setup budget')
            time.sleep(.1)


def verify_setup_recovery(root, events, version, program_before, history_before, *,
                          process_running, protected_hashes, expected_hashes,
                          admission_probe, program_root=None):
    def read(name): return json.loads((events/name).read_text(encoding='utf-8'))
    peer, bridge, owners = read('peer-result.json'), read('bridge-result.json'), read('old-processes.json')
    frames = re.findall(r'File "([^"]+)", line \d+, in (\w+)', peer.get('traceback',''))
    frames = [(path.replace('\\','/').split('/')[-1], function) for path,function in frames]
    if (peer.get('exception_type') != 'UpdateBusy' or not frames or
            frames[0] != ('update_smoke.py','inspect') or frames[-1] != ('update_admission.py','_admission') or
            'shaq_daily_oracle.update_admission.UpdateBusy:' not in peer.get('traceback','') or
            bridge.get('exception_type') != 'TimeoutError' or
            bridge.get('error') != 'Missing installed acceptance event: analysis-held.json' or
            bridge.get('waiting_stages') != [] or bridge.get('download_verified') is not True):
        raise RuntimeError('Not the initial fixture admission mutex failure')
    for row, stage in ((peer,'peer'), (bridge,'bridge')):
        if (row.get('status') != 'failed' or row.get('stage') != stage or row.get('actual_version') != version or
                row.get('preserved_hashes') is not True or row.get('pages') != ['run','editor','history']):
            raise RuntimeError('Setup failure lacks rendered, preserved original application evidence')
    pids = [bridge.get('pid'),peer.get('pid')]
    if (any(type(pid) is not int or pid <= 0 for pid in pids) or len(set(pids)) != 2 or
            owners.get('pids') != pids or bridge.get('peer_pid') != pids[1] or
            any(process_running(pid) for pid in pids)):
        raise RuntimeError('All recorded owned GUI processes must have exited')
    allowed = {'peer-result.json','bridge-result.json','old-processes.json','peer.log'}
    if any(path.name not in allowed for path in events.iterdir()):
        raise RuntimeError('Work or transition events already exist; setup retry forbidden')
    def unchanged():
        admission = root/'data/update-admission'
        if (admission/'installing.json').exists() or (admission/'gui-sessions/request.json').exists():
            raise RuntimeError('Install intent or GUI transition request exists')
        if history_hashes(root) != history_before:
            raise RuntimeError('Update history changed before setup recovery')
        if not program_before or file_hashes(program_root or root/'installed') != program_before:
            raise RuntimeError('Installed application bytes changed')
        if not expected_hashes or protected_hashes() != expected_hashes:
            raise RuntimeError('Preserved user data bytes changed')
    unchanged()
    admission_probe()  # A timeout alone is never classified as recovered.
    unchanged()
    return {'recovered_admission':True, 'owned_exited':pids, 'application_unchanged':True,
            'protected_data_unchanged':True, 'history_unchanged':True, 'failed_events':events.name}


def run_with_setup_recovery(run, label, command, configuration, verify, recoveries):
    try:
        run(label, command, timeout=300)
    except RuntimeError:
        evidence = verify()  # Any non-matching failure exits without retry.
        config = json.loads(configuration.read_text(encoding='utf-8'))
        original = config['events_directory']
        retry = original+'-retry-1'
        if Path(original).name != original or (configuration.parent/retry).exists():
            raise RuntimeError('Fresh, local setup retry namespace required')
        evidence.update(first_stage=label, retry_stage=label+'-retry-1', retry_events=retry)
        recoveries.append(evidence)
        # Persist recovery authorization before re-launch, including on failure.
        (configuration.parent/('setup-recovery-'+original+'.json')).write_text(json.dumps(evidence,indent=2),encoding='utf-8')
        config['events_directory'] = retry
        from shaq_daily_oracle.settings import _atomic_json
        _atomic_json(configuration,config)
        run(label+'-retry-1', command, timeout=300)  # Never inside a retry loop.
    return configuration.parent/json.loads(configuration.read_text(encoding='utf-8'))['events_directory']
