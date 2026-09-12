import copy
import unittest
from unittest.mock import patch

from shaq_daily_oracle import run_comparison


def batch(key='team/main', *, evidence='e' * 64, model='m' * 64, rules='r' * 64):
    return {
        'batch_id': 'LAB-2026-09-09-example',
        'manifest': {'batch_identity': {'evidence_hash': evidence,
                                      'model_profile_hash': model}},
        'evidence': {'evidence_hash': evidence, 'as_of_et': '2026-09-09T08:50:00-04:00',
                     'candidates': [{'symbol': 'AAA', 'premarket_return': .02}]},
        'variants': {key: {'variant': {'label': '方法'}, 'model_profile_sha256': model,
                          'model_call_audits': [{'request_policy_sha256':'p'*64}],
                          'predictions': [{'symbol': 'AAA', 'direction': 'bullish'}],
                          'reports_by_symbol': {'AAA': []}, 'integration_audit': {}}},
        'skill_snapshots': {key: {'documents': {'skills/event/SKILL.md': 'original'}}},
        'virtual_accounts': {'results': [{'variant_key': key, 'execution_policy_hash': rules,
                                         'engine': 'zipline', 'engine_version': '3.1.1',
                                         'status': 'provisional', 'net_pnl': -2.5}]},
    }


class RunComparisonTests(unittest.TestCase):
    def test_compares_frozen_method_contents_not_author_names_without_mutation(self):
        left, right = batch(), batch('other/renamed')
        before = copy.deepcopy((left, right))
        value = run_comparison.compare_runs(left, 'team/main', right, 'other/renamed')
        self.assertEqual(value['dimensions']['method']['status'], 'same')
        self.assertEqual(value['dimensions']['model']['status'], 'same')
        self.assertTrue(value['controlled_method_comparison'])
        self.assertEqual((left, right), before)

    def test_changed_methods_show_actual_paths_and_direction_difference(self):
        left, right = batch(), batch('team/alternative')
        right['skill_snapshots']['team/alternative']['documents']['skills/event/SKILL.md'] = 'changed'
        right['variants']['team/alternative']['predictions'][0]['direction'] = 'bearish'
        value = run_comparison.compare_runs(left, 'team/main', right, 'team/alternative')
        self.assertEqual(value['dimensions']['method']['status'], 'different')
        self.assertEqual(value['changed_files'][0]['path'], 'skills/event/SKILL.md')
        self.assertIn('+changed', value['changed_files'][0]['diff'])
        self.assertEqual(value['stocks'][0]['left'], 'bullish')
        self.assertEqual(value['stocks'][0]['right'], 'bearish')
        self.assertEqual(value['outcomes']['right']['net_pnl'], -2.5)

    def test_different_models_evidence_rules_or_candidates_are_not_pure_method_comparison(self):
        for dimension in ('model', 'data', 'trading_rules', 'candidates', 'trade_date'):
            with self.subTest(dimension=dimension):
                left, right = batch(), batch('team/alternative')
                if dimension == 'model':
                    right['variants']['team/alternative']['model_profile_sha256'] = 'b' * 64
                elif dimension == 'data':
                    right['evidence']['evidence_hash'] = 'b' * 64
                elif dimension == 'trading_rules':
                    right['virtual_accounts']['results'][0]['execution_policy_hash'] = 'b' * 64
                elif dimension == 'candidates':
                    right['variants']['team/alternative']['candidate_intake'] = {'candidates': [{'symbol': 'BBB'}]}
                else:
                    right['evidence']['as_of_et'] = '2026-09-10T08:50:00-04:00'
                value = run_comparison.compare_runs(left, 'team/main', right, 'team/alternative')
                self.assertEqual(value['dimensions'][dimension]['status'], 'different')
                self.assertFalse(value['controlled_method_comparison'])

    def test_missing_identities_are_unknown_not_equal(self):
        left, right = batch(), batch('team/alternative')
        for source, key in ((left, 'team/main'), (right, 'team/alternative')):
            source['variants'][key].pop('model_profile_sha256')
            source['manifest'] = {}
            source['evidence'] = {}
            source['skill_snapshots'] = {}
            source['virtual_accounts'] = {}
        value = run_comparison.compare_runs(left, 'team/main', right, 'team/alternative')
        self.assertTrue(all(item['status'] == 'unknown' for item in value['dimensions'].values()))
        self.assertFalse(value['controlled_method_comparison'])
        self.assertIsNone(value['outcomes']['left']['net_pnl'])

    def test_absent_variant_is_error_instead_of_comparing_empty_objects(self):
        with self.assertRaisesRegex(ValueError, '版本'):
            run_comparison.compare_runs(batch(), 'team/missing', batch(), 'team/main')

    def test_effective_request_policy_difference_prevents_controlled_claim(self):
        left,right=batch(),batch('team/alternative')
        right['variants']['team/alternative']['model_call_audits'][0]['request_policy_sha256']='q'*64
        value=run_comparison.compare_runs(left,'team/main',right,'team/alternative')
        self.assertEqual(value['dimensions']['model']['status'],'different')
        self.assertFalse(value['controlled_method_comparison'])

    def test_bridge_compares_only_verified_batch_details(self):
        from shaq_daily_oracle.desktop import DesktopBridge
        from types import SimpleNamespace
        bridge = object.__new__(DesktopBridge)
        bridge.lab = SimpleNamespace(batch_detail=lambda identifier: batch() if identifier == 'left' else batch('team/alternative'))
        value = bridge.compare_research_runs(
            {'batch_id': 'left', 'variant_key': 'team/main'},
            {'batch_id': 'right', 'variant_key': 'team/alternative'})
        self.assertTrue(value['ok'])
        self.assertTrue(value['value']['controlled_method_comparison'])
