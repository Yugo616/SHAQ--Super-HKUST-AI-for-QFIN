import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from shaq_daily_oracle.research_dashboard import display_model


class ModelDisplayTests(unittest.TestCase):
    def test_display_annotation_does_not_invalidate_account_cache_input(self):
        from shaq_daily_oracle.research_dashboard import ResearchDashboardIndex
        from shaq_daily_oracle.minute_settlements import MinuteStore
        with tempfile.TemporaryDirectory() as tmp:
            index=ResearchDashboardIndex(batches_root=Path(tmp)/'batches',database=Path(tmp)/'index')
            row={'batch_id':'b','variant_key':'v','series_key':'s','trade_date':'2026-09-11',
                 'variant_result_sha256':'hash','predictions':[], 'model':'gpt6astra','model_for_account':'subscription-default'}
            with patch.object(MinuteStore,'snapshot',return_value={}):
                actual=index.account_rows([row])[0]
            self.assertEqual(actual['model'],'subscription-default')
            self.assertNotIn('model_for_account',actual)
            self.assertEqual(row['model'],'gpt6astra')

    def test_placeholder_does_not_hide_actual_or_requested_model(self):
        variant = {'model_name':'subscription-default','model_call_audits':[{'cache_key':'ours'}]}
        calls = [{'cache_key':'other','response_model':'wrong'},
                 {'cache_key':'ours','response_model':'','requested_model':'gpt-5.6-terra'}]
        self.assertEqual(display_model(variant,calls)['name'],'gpt-5.6-terra')
        calls[1]['response_model']='gpt-5.6-terra-20260917'
        self.assertEqual(display_model(variant,calls)['source'],'provider')
        self.assertEqual(display_model(variant,calls)['name'],'gpt-5.6-terra-20260917')

    def test_historical_annotation_never_overrides_recorded_model(self):
        annotation={'model':'gpt6astra','source':'user_confirmation'}
        self.assertEqual(display_model({'model_name':'subscription-default'},[],annotation),
                         {'name':'gpt6astra','source':'user_confirmation'})
        self.assertEqual(display_model({'model_name':'qwen3'},[],annotation)['name'],'qwen3')
        self.assertEqual(display_model({'model_name':'subscription-default'},[])['name'],'')
