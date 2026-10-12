"""Research timing policy, separate from model and trading identities.

Evidence must be sealed before the decision. The decision must be saved before
(but not including) the exchange's regular-session open.
Assessments are derived; existing frozen results are never rewritten.
"""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from .market_calendar import market_session


POLICY_ID = 'decision-before-market-open-v2'
ET = ZoneInfo('America/New_York')


def decision_order(completed_at, batch_id, variant_key):
    """One deterministic ordering for scoring and account selection."""
    try:
        completed = _stamp(completed_at)
    except (TypeError, ValueError):
        completed = datetime.min.replace(tzinfo=timezone.utc)
    return completed, str(batch_id), str(variant_key)


def _stamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError('Timing evidence requires a timezone')
    return parsed.astimezone(ET)


def assess_timing(evidence, completed_at, *, trade_date=None, original_score_eligible=None):
    assessment = {
        'policy_id': POLICY_ID, 'eligible': False, 'reason': 'missing_or_invalid_timestamps',
        'publication_deadline_et': None,
        'original_score_eligible': original_score_eligible, 'reassessed': False,
        'evidence_hash': evidence.get('evidence_hash'),
    }
    try:
        frozen = _stamp(evidence['as_of_et'])
        scheduled = _stamp(evidence['scheduled_cutoff_et'])
        completed = _stamp(completed_at)
        day = date.fromisoformat(trade_date) if trade_date else scheduled.date()
        session = market_session(day)
        if session is None:
            assessment['reason'] = 'market_closed'
            return assessment
        assessment['publication_deadline_et'] = session.market_open.isoformat()
        collected = evidence.get('provider_manifest', {}).get('collection_completed_at_et')
        observations = [frozen] + ([_stamp(collected)] if collected else [])
        if scheduled.date() != day or any(x.date() != day for x in observations) or completed.date() != day:
            assessment['reason'] = 'wrong_session_date'
        elif completed < max(observations):
            assessment['reason'] = 'completion_precedes_evidence'
        elif completed >= session.market_open:
            assessment['reason'] = 'analysis_not_completed_before_open'
        else:
            assessment.update(eligible=True, reason='evidence_before_decision_completed_before_open')
    except (KeyError, TypeError, ValueError):
        pass
    assessment['reassessed'] = (original_score_eligible is not None
                                and original_score_eligible != assessment['eligible'])
    return assessment
