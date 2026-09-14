"""Append-only display corrections for interrupted jobs with terminal failure proof."""
from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import date, datetime

from filelock import FileLock, Timeout

from .hashing import sha256_file, sha256_payload


@contextmanager
def _inactive_job(root, job_id):
    if not re.fullmatch(r'job-[A-Za-z0-9_-]+', job_id):
        raise ValueError('invalid job identity')
    # Same order as the scheduler: never correct a live job or live automatic run.
    try:
        with FileLock(str(root / 'schedule.lock'), timeout=0):
            with FileLock(str(root / 'jobs' / (job_id + '.lock')), timeout=0):
                yield
    except Timeout as exc:
        raise ValueError('job or automatic scheduler is active; correction refused') from exc


def _verified_correction(root, *, job_id, trade_date, job_sha256, automatic_run_sha256):
    if date.fromisoformat(trade_date).isoformat() != trade_date:
        raise ValueError('invalid automatic run date')
    job_path = root / 'jobs' / (job_id + '.json')
    automatic_path = root / 'automatic_runs' / (trade_date + '.json')
    if sha256_file(job_path) != job_sha256 or sha256_file(automatic_path) != automatic_run_sha256:
        raise ValueError('original job or automatic run hash mismatch')
    job = json.loads(job_path.read_text(encoding='utf-8'))
    failure = json.loads(automatic_path.read_text(encoding='utf-8'))
    if (job.get('job_id') != job_id or job.get('status') not in {'queued', 'running'}
            or job.get('completed_at_et') or failure.get('job_id') != job_id
            or failure.get('status') != 'failed' or not failure.get('error_type')
            or not failure.get('message') or not job.get('started_at_et')
            or failure.get('started_at_et') != job['started_at_et']):
        raise ValueError('automatic failure proof does not match interrupted job')
    started = datetime.fromisoformat(job['started_at_et'])
    completed = datetime.fromisoformat(failure['completed_at_et'])
    if (started.tzinfo is None or completed.tzinfo is None or completed < started
            or started.date().isoformat() != trade_date):
        raise ValueError('automatic failure proof has invalid times')
    return {'schema_version': 1, 'job_id': job_id, 'trade_date': trade_date,
            'job_sha256': job_sha256, 'automatic_run_sha256': automatic_run_sha256,
            'status': 'failed', 'started_at_et': job['started_at_et'],
            'completed_at_et': failure['completed_at_et'],
            'error_type': failure['error_type'], 'message': failure['message']}


def append_correction(root, **request):
    with _inactive_job(root, request['job_id']):
        correction = _verified_correction(root, **request)
        destination = root / 'job_corrections' / (
            request['job_id'] + '-' + sha256_payload(correction) + '.json')
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Exclusive create: never rewrite original artifacts or an earlier correction.
            with destination.open('x', encoding='utf-8') as handle:
                handle.write(json.dumps(correction, ensure_ascii=False, sort_keys=True, indent=2) + '\n')
        except FileExistsError:
            if json.loads(destination.read_text(encoding='utf-8')) != correction:
                raise ValueError('existing correction content mismatch')
        return correction


def corrected_status(root, row):
    """Read-only overlay; recheck original bytes and locks every time it is used."""
    job_id = row['job_id']
    if row.get('status') not in {'queued', 'running'}:
        return row
    candidates = root / 'job_corrections'
    if not candidates.is_dir():
        return row
    try:
        with _inactive_job(root, job_id):
            for path in candidates.glob(job_id + '-*.json'):
                try:
                    correction = json.loads(path.read_text(encoding='utf-8'))
                    request = {key: correction[key] for key in (
                        'job_id', 'trade_date', 'job_sha256', 'automatic_run_sha256')}
                    verified = _verified_correction(root, **request)
                    if (correction != verified or request['job_id'] != job_id
                            or path.name != job_id + '-' + sha256_payload(verified) + '.json'
                            or row.get('started_at_et') != verified['started_at_et']):
                        continue
                    return {**row, **{key: verified[key] for key in (
                        'status', 'completed_at_et', 'error_type', 'message')},
                        'status_correction': {'path': path.name,
                            'job_sha256': verified['job_sha256'],
                            'automatic_run_sha256': verified['automatic_run_sha256']}}
                except (ValueError, KeyError, TypeError, OSError):
                    continue
    except (ValueError, OSError):
        pass
    return row
