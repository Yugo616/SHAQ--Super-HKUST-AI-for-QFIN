import json
import tempfile
import unittest
from pathlib import Path

from shaq_daily_oracle.research_batch import freeze_evidence_bundle, _adversary_prompt
from shaq_daily_oracle.review_timing import review_timing_context
from shaq_daily_oracle.synthesis import synthesis_prompt
import test_research_batch as fixture


class ReviewTimingTests(unittest.TestCase):
    def test_review_context_change_reuses_all_domain_calls(self):
        from shaq_daily_oracle.research_batch import run_variant, ContentAddressedModelCache
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); setup=fixture.ResearchBatchTests()
            registry=setup.registry(root); model=fixture.FakeModel()
            common=dict(variant=setup.main_variant(registry), evidence=setup.evidence(root),
                registry=registry, profile=setup.profile(), secret='',
                cache=ContentAddressedModelCache(root/'cache'), integration_policy=setup.policy(),
                caller=model)
            before=run_variant(**common,output_root=root/'old'/'variants')
            old_calls=model.calls
            after=run_variant(**common,output_root=root/'new'/'variants',
                              review_timing_contract_version=1)
            self.assertEqual(model.calls,old_calls+1)
            self.assertEqual(before['reports_by_symbol'],after['reports_by_symbol'])
            self.assertEqual(before['predictions'],after['predictions'])

    def test_late_capture_keeps_market_observation_separate_and_no_prices(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = freeze_evidence_bundle(root=Path(tmp)/'evidence',
                as_of_et='2026-10-09T10:15:00-04:00',
                scheduled_cutoff_et='2026-10-09T08:50:00-04:00',
                cutoff_status='late_research_only', candidates=[{'symbol':'AAPL'}],
                records=[{'evidence_id':'ev_test','domain':'price_volume','provider':'test',
                    'source_uri':'https://example.test/bars','scope_symbols':['AAPL'],
                    'captured_at':'2026-10-09T10:15:00-04:00','raw_file_path':'raw/data.json',
                    'published_at':'2026-10-09T07:00:00-04:00',
                    'root_component_type':'stock_price_volume'}],
                files={'raw/data.json':json.dumps({'bars':[
                    {'timestamp':'2026-10-09T08:31:00-04:00','close':123},
                    {'timestamp':'2026-10-09T08:33:00-04:00','close':124}],
                    'acceptance_time':'2026-10-09T07:00:00-04:00',
                    'asof_session':'2026-10-08','quote_as_of':'2026-10-08T16:00:00-04:00',
                    'formal_cutoff_eligible':False}).encode()}, provider_manifest={})
            reports={'AAPL':[{'evidence_ids':['ev_test']}]}
            result=review_timing_context(evidence,reports)
            self.assertEqual(result['scheduled_cutoff_et'],'2026-10-09T08:50:00-04:00')
            self.assertEqual(result['bundle_captured_at_et'],'2026-10-09T10:15:00-04:00')
            row=result['sources'][0]
            self.assertEqual(row['published_at'],'2026-10-09T07:00:00-04:00')
            self.assertEqual(row['timing_fields']['acceptance_time'],'2026-10-09T07:00:00-04:00')
            self.assertEqual(row['timing_fields']['asof_session'],'2026-10-08')
            self.assertEqual(row['timing_fields']['quote_as_of'],'2026-10-08T16:00:00-04:00')
            self.assertEqual(row['timing_fields']['bars[].timestamp']['last_in_source'],
                             '2026-10-09T08:33:00-04:00')
            self.assertNotIn('close',json.dumps(result))
            self.assertFalse(row['timing_fields']['formal_cutoff_eligible'])
            (evidence.root/'raw/data.json').write_text('{}')
            with self.assertRaises(ValueError): review_timing_context(evidence,reports)

    def test_prompt_contract_is_optional_and_does_not_erase_integrity_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry=fixture.ResearchBatchTests().registry(Path(tmp))
            docs=registry.effective_skills('main','team')
            reports={'AAPL':[]}
            old=_adversary_prompt(reports_by_symbol=reports,documents=docs)
            explicit=_adversary_prompt(reports_by_symbol=reports,documents=docs,timing_context=None)
            self.assertEqual(old,explicit)
            context={'scheduled_cutoff_et':'08:50','bundle_captured_at_et':'10:15','sources':[]}
            new=_adversary_prompt(reports_by_symbol=reports,documents=docs,timing_context=context)
            self.assertIn('EVIDENCE TIMING',new)
            self.assertIn('10:15',new)
            self.assertIn('leakage',new)
            self.assertEqual(json.loads(new.split('REPORTS:\n',1)[1]),reports)
            synth=synthesis_prompt(reports=reports,adversary={},documents=docs,as_of_et='10:15',
                maximum_predictions=3,timing_context=context)
            self.assertIn('Integrity vetoes cannot be bypassed',synth)
            self.assertIn('evidence_timing',synth)
