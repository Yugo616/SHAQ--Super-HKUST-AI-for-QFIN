import copy
import unittest
from shaq_daily_oracle.replay_summary import candidate_summary, compare_versions


class ReplaySummaryTests(unittest.TestCase):
    def test_final_result_does_not_invent_a_cause_or_mutate_prediction(self):
        variant = {'predictions': [{'symbol': 'X', 'direction': 'bullish'}],
                   'reports_by_symbol': {'X': [{'domain': 'price_volume', 'verdict': 'bullish',
                       'thesis': 'Buying continued', 'antithesis': 'Pressure may fade', 'evidence_ids': ['e1']}]}}
        before = copy.deepcopy(variant)
        label = {'status': 'final', 'official_unadjusted_open': 100, 'official_unadjusted_close': 99}
        result = candidate_summary(variant, 'X', label)
        self.assertEqual(result['correct'], False)
        self.assertEqual(result['return_pct'], -1)
        self.assertEqual(result['basis'][0]['evidence_ids'], ['e1'])
        self.assertIn('不能确定', result['explanation'])
        self.assertEqual(variant, before)
        label['official_unadjusted_close'] = 100
        self.assertFalse(candidate_summary(variant, 'X', label)['correct'])

    def test_provisional_and_empty_are_not_scored(self):
        v = {'predictions': [{'symbol': 'X', 'direction': 'bearish'}]}
        self.assertIsNone(candidate_summary(v, 'X', {'status': 'provisional'})['correct'])
        self.assertIsNone(candidate_summary({}, 'X', {'status': 'final',
            'official_unadjusted_open': 100, 'official_unadjusted_close': 90})['correct'])

    def test_comparison_reports_method_and_selection_changes(self):
        left = {'predictions': [], 'model_profile_sha256': 'a', 'reports_by_symbol': {'X': []}}
        right = {'predictions': [{'symbol': 'X', 'direction': 'bearish'}], 'model_profile_sha256': 'b'}
        result = compare_versions(left, right, {'decision/decision.js': 'a'}, {'decision/decision.js': 'b'})
        self.assertFalse(result['same_model'])
        self.assertEqual(result['changed_modules'], ['decision'])
        self.assertEqual(result['stocks'][0]['left'], 'not_published')
        self.assertEqual(result['stocks'][0]['right'], 'bearish')
