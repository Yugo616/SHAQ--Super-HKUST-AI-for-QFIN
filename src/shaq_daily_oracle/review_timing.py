"""Source timing for late research reviews, not a claim of prospective eligibility."""
import json
from collections import defaultdict
from pathlib import Path

from .hashing import sha256_file


TIMING_GUIDANCE = (
    'This is a late research reconstruction, not an on-time forecast. Report as_of_et '
    'is the evidence bundle capture time, not necessarily the market observation time. '
    'Use the supplied source timing fields to distinguish download time from observation '
    'or publication time. Late capture alone does not establish result leakage, and does '
    'not establish historical availability either. Actual observations after the scheduled '
    'cutoff, missing publication timing and unverified revisions must remain explicit '
    'limitations; do not assume all sources are temporally valid. Integrity rules remain unchanged.'
)

# Explicit temporal source fields; no prices, future outcomes, credentials or prose.
_FIELDS = frozenset({
    'timestamp', 'date', 'DATE', 'published_at', 'published_at_et', 'first_published_at',
    'accepted_at', 'acceptance_datetime', 'acceptance_time', 'filing_date', 'observation_date',
    'quote_as_of', 'snapshot_session', 'open_interest_as_of', 'asof_session',
    'first_bar_start_et', 'last_bar_start_et', 'first_minute_start_et',
    'last_complete_minute_end_et', 'first_quote_time', 'last_quote_time',
    'first_trade_time', 'last_trade_time', 'first_trade_et', 'last_trade_et',
    'first_observation_at', 'last_observation_at', 'observed_at', 'as_of_et',
    'captured_at', 'captured_at_et', 'capture_completed_at', 'requested_at_et',
    'requested_start', 'requested_end', 'requested_cutoff', 'cutoff', 'cutoff_et',
    'session_date', 'data_semantics', 'inference_scope', 'formal_cutoff_eligible',
    'post_cutoff_replay', 'observation_period',
})


def _timing_fields(content):
    values = defaultdict(list)
    def walk(value, path=''):
        if isinstance(value, dict):
            for key, item in value.items():
                child = f'{path}.{key}' if path else key
                if key in _FIELDS and isinstance(item, (str, bool, int, float)):
                    values[child].append(item)
                elif isinstance(item, (dict, list)):
                    walk(item, child)
        elif isinstance(value, list):
            for item in value:
                walk(item, path + '[]')
    walk(content)
    return {key: rows[0] if len(rows) == 1 else {
        'count': len(rows), 'first_in_source': rows[0], 'last_in_source': rows[-1],
    } for key, rows in sorted(values.items())}


def review_timing_context(evidence, reports):
    cited = {eid for rows in reports.values() for row in rows
             for eid in row.get('evidence_ids', [])}
    sources = []
    for record in sorted(evidence.lineage['records'], key=lambda r: r['evidence_id']):
        if record['evidence_id'] not in cited:
            continue
        path = Path(record['raw_file_path'])
        if sha256_file(path) != record['raw_sha256']:
            raise ValueError('evidence changed before temporal review')
        try:
            content = json.loads(path.read_text(encoding='utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            content = {}
        sources.append({'evidence_id': record['evidence_id'],
            'provider': record['provider'], 'scope_symbols': record.get('scope_symbols', ['*']),
            'captured_at': record['captured_at'], 'published_at': record.get('published_at'),
            'timing_fields': _timing_fields(content)})
    return {'scheduled_cutoff_et': evidence.manifest['scheduled_cutoff_et'],
        'bundle_captured_at_et': evidence.manifest['as_of_et'],
        'cutoff_status': evidence.manifest['cutoff_status'],
        'sources': sources}
