import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shaq_daily_oracle.research_dashboard import ResearchDashboardIndex
from shaq_daily_oracle.virtual_accounts import _late


class PreopenCompletionTests(unittest.TestCase):
    def detail(self, completed='09:00:16', frozen='08:46:28', day='2026-09-16', offset='-04:00'):
        return {
            'evidence': {
                'as_of_et': f'{day}T{frozen}{offset}',
                'scheduled_cutoff_et': f'{day}T08:50:00{offset}',
                'cutoff_status': 'on_time',
                'provider_manifest': {'collection_completed_at_et': f'{day}T{frozen}{offset}'},
            },
            'variants': {'team/shadow': {
                'variant': {'version_sha256': 'method', 'label': 'Shadow'},
                'model_profile_sha256': 'model', 'score_eligible': False,
                'completed_at_et': f'{day}T{completed}{offset}', 'predictions': [],
            }}, 'labels': {}, 'status': {},
        }

    def row(self, detail, day='2026-09-16'):
        before = copy.deepcopy(detail)
        with tempfile.TemporaryDirectory() as tmp:
            index = ResearchDashboardIndex(batches_root=Path(tmp)/'batches', database=Path(tmp)/'index.db')
            with patch.object(index, 'batch_detail', return_value=detail):
                rows = index._daily_results([dict(batch_id='batch', trade_date=day, source_valid=True)])
        self.assertEqual(detail, before, 'Reassessment must not rewrite the frozen record')
        return rows[0]

    def test_after_nine_before_open_is_eligible_with_explicit_reassessment(self):
        row = self.row(self.detail())
        self.assertTrue(row['score_eligible'])
        self.assertEqual(row['publication_deadline_et'], '2026-09-16T09:30:00-04:00')
        self.assertFalse(row['timing_assessment']['original_score_eligible'])
        self.assertTrue(row['timing_assessment']['reassessed'])

    def test_open_is_exclusive_boundary(self):
        for completed, expected in [('09:29:59', True), ('09:30:00', False), ('09:30:01', False)]:
            with self.subTest(completed=completed):
                self.assertEqual(self.row(self.detail(completed=completed))['score_eligible'], expected)

    def test_evidence_may_be_collected_after_old_cutoff_before_decision(self):
        for frozen, expected in [('08:50:00', True), ('08:50:01', True), ('09:20:00', True)]:
            with self.subTest(frozen=frozen):
                detail = self.detail(frozen=frozen, completed='09:29:59')
                detail['evidence']['cutoff_status'] = 'late_research_only'
                self.assertEqual(self.row(detail)['score_eligible'], expected)

    def test_collection_completion_cannot_be_hidden_by_earlier_asof(self):
        detail = self.detail()
        detail['evidence']['provider_manifest']['collection_completed_at_et'] = '2026-09-16T09:01:00-04:00'
        self.assertFalse(self.row(detail)['score_eligible'])

    def test_latest_success_before_open_wins_not_late_or_failed_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = ResearchDashboardIndex(batches_root=Path(tmp)/'batches', database=Path(tmp)/'index.db')
            details = {'first': self.detail(completed='08:55:00'),
                       'last': self.detail(completed='09:20:00', frozen='09:10:00'),
                       'late': self.detail(completed='09:31:00')}
            batches = [dict(batch_id=k, trade_date='2026-09-16', source_valid=True) for k in details]
            with patch.object(index, 'batch_detail', side_effect=lambda k: details[k]):
                rows = {r['batch_id']: r for r in index._daily_results(batches)}
            self.assertTrue(rows['last']['score_eligible'])
            self.assertFalse(rows['first']['score_eligible'])
            self.assertTrue(rows['first']['can_delete'])
            self.assertEqual(rows['first']['superseded_by'], 'last')
            self.assertFalse(rows['last']['can_delete'])
            self.assertFalse(rows['late']['can_delete'])

    def test_verified_assessment_overrides_legacy_late_collection_label(self):
        row = self.row(self.detail(frozen='08:56:00', completed='09:03:00'))
        row['cutoff_status'] = 'late_research_only'
        self.assertFalse(_late(row))

    def test_same_batch_alias_selects_latest_variant_not_first_name(self):
        detail = self.detail(completed='08:55:00')
        detail['variants']['team/z-alias'] = copy.deepcopy(detail['variants']['team/shadow'])
        detail['variants']['team/z-alias']['completed_at_et'] = '2026-09-16T09:20:00-04:00'
        with tempfile.TemporaryDirectory() as tmp:
            index = ResearchDashboardIndex(batches_root=Path(tmp)/'batches', database=Path(tmp)/'index.db')
            with patch.object(index, 'batch_detail', return_value=detail):
                rows = index._daily_results([dict(batch_id='batch', trade_date='2026-09-16', source_valid=True)])
        self.assertEqual([r['variant_key'] for r in rows if r['score_eligible']], ['team/z-alias'])
        self.assertTrue(next(r for r in rows if r['variant_key']=='team/shadow')['can_delete'])

    def test_missing_freeze_proof_cannot_requalify_legacy_result(self):
        detail = self.detail()
        del detail['evidence']['as_of_et']
        self.assertFalse(self.row(detail)['score_eligible'])

    def test_result_cannot_predate_evidence_or_use_another_date(self):
        self.assertFalse(self.row(self.detail(completed='08:45:00'))['score_eligible'])
        detail = self.detail()
        detail['variants']['team/shadow']['completed_at_et'] = '2026-09-17T09:00:00-04:00'
        self.assertFalse(self.row(detail)['score_eligible'])

    def test_holiday_and_winter_timezone(self):
        self.assertFalse(self.row(self.detail(day='2026-09-07'), day='2026-09-07')['score_eligible'])
        row = self.row(self.detail(day='2026-11-27', offset='-05:00'), day='2026-11-27')
        self.assertTrue(row['score_eligible'])
        self.assertEqual(row['publication_deadline_et'], '2026-11-27T09:30:00-05:00')

    def test_account_uses_open_when_no_explicit_deadline(self):
        row = dict(trade_date='2026-09-16', cutoff_status='on_time', completed_at_et='2026-09-16T09:00:16-04:00')
        self.assertFalse(_late(row))
        row['completed_at_et'] = '2026-09-16T09:30:00-04:00'
        self.assertTrue(_late(row))
