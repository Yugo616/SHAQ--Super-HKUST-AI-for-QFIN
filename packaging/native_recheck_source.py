"""Bind a single public-base recheck to a fully built immutable Mac artifact."""
import argparse
import json
from pathlib import Path
import re

PUBLIC_STEP = 'Apply final Mac delta from verified actually published disk image'
REQUIRED_STEPS = (
    'Validate source privacy and eight Skills before building',
    'Validate all application tests',
    'Check native desktop layout at normal and narrow widths',
    'Full installed native delta update and replay acceptance',
    'Build macOS disk image',
    'Install from DMG, settle both methods, reopen and inspect native window',
    'Retain installers, checksums, provenance, native sources and acceptance evidence',
)


def validate(run, job, artifact, *, run_id, job_id, artifact_id, application_sha, platform):
    if platform not in ('macOS-Apple-Silicon', 'macOS-Intel'):
        raise ValueError('A native Mac platform is required')
    if not re.fullmatch('[0-9a-f]{40}', application_sha):
        raise ValueError('A full immutable application commit is required')
    if (run.get('id') != run_id or run.get('head_sha') != application_sha
            or job.get('id') != job_id or job.get('run_id') != run_id
            or platform not in job.get('name', '') or job.get('conclusion') != 'failure'):
        raise ValueError('Source job or commit identity mismatch')
    if (artifact.get('id') != artifact_id or artifact.get('expired') is not False
            or artifact.get('name') != 'SHAQ-Daily-Oracle-Lab-' + platform
            or artifact.get('workflow_run', {}).get('id') != run_id
            or artifact.get('workflow_run', {}).get('head_sha') != application_sha):
        raise ValueError('Artifact provenance does not match the frozen application')
    steps = {step['name']: step.get('conclusion') for step in job.get('steps', [])}
    if any(steps.get(name) != 'success' for name in REQUIRED_STEPS):
        raise ValueError('A previous native acceptance gate did not pass')
    if steps.get(PUBLIC_STEP) != 'failure' or any(
            outcome in ('failure', 'cancelled', 'timed_out') and name != PUBLIC_STEP
            for name, outcome in steps.items()):
        raise ValueError('Only the final public-base gate may be rechecked')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--metadata', type=Path, required=True)
    parser.add_argument('--run-id', type=int, required=True)
    parser.add_argument('--job-id', type=int, required=True)
    parser.add_argument('--artifact-id', type=int, required=True)
    parser.add_argument('--application-sha', required=True)
    parser.add_argument('--platform', required=True)
    args = parser.parse_args()
    documents = [json.loads((args.metadata / (name + '.json')).read_text())
                 for name in ('run', 'job', 'artifact')]
    validate(*documents, run_id=args.run_id, job_id=args.job_id,
             artifact_id=args.artifact_id, application_sha=args.application_sha,
             platform=args.platform)
    print('Exact source, artifact and all preceding native acceptance gates verified.')


if __name__ == '__main__': main()
