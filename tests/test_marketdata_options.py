import unittest
import tempfile
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch
import httpx
import test_research_lab_foundation as foundation
from shaq_daily_oracle import marketdata_options
from shaq_daily_oracle.research_settings import ResearchSettingsStore
from shaq_daily_oracle.lab_service import LabService

ET=ZoneInfo('America/New_York')
NOW=datetime(2026,10,8,8,40,tzinfo=ET)
STAMP=int(datetime(2026,10,6,16,tzinfo=ET).timestamp())


def response_body():
    return {'s':'ok','optionSymbol':['DVA261016C00180000','DVA261016P00180000'],
        'underlying':['DVA','DVA'],'side':['call','put'],'strike':[180,180],
        'expiration':[int(datetime(2026,10,16,tzinfo=ET).timestamp())]*2,
        'bid':[3.0,4.0],'ask':[4.0,5.0],'updated':[STAMP,STAMP],
        'underlyingPrice':[179.0,179.0],'volume':[10,20],'openInterest':[80,90], 'iv':[None,None]}


class OptionTests(unittest.TestCase):
    def test_connection_saves_only_after_quote_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            lab=LabService(foundation.ResearchLabFoundationTests().paths(Path(directory)))
            receipt={'status':'collected','contracts':[{'bid':3,'ask':4}],
                     'snapshot_session':'2026-10-06','captured_at':NOW.isoformat(),'raw':b'fixture'}
            with patch('shaq_daily_oracle.marketdata_options.collect_historical_chain',return_value=receipt):
                saved=lab.save_setup({'data_connection_action':'connect_marketdata','marketdata_token':'fixture-secret'})
            self.assertTrue(saved['data_profile']['marketdata_options_enabled'])
            self.assertEqual(lab.settings.get_marketdata_token(),'fixture-secret')
            self.assertNotIn('fixture-secret',lab.paths.research_settings_file.read_text())
            with patch('shaq_daily_oracle.marketdata_options.collect_historical_chain',side_effect=ValueError('测试失败')):
                with self.assertRaises(ValueError):
                    lab.save_setup({'data_connection_action':'connect_marketdata','marketdata_token':'bad-secret'})
            self.assertEqual(lab.settings.get_marketdata_token(),'fixture-secret')
    def collect(self, body, status=200, *, captured=NOW, research_replay=False):
        def request(r):
            self.assertEqual(r.url.host,'api.marketdata.app')
            self.assertEqual(r.url.params['date'],'2026-10-06')
            self.assertNotIn('token',r.url.params)
            return httpx.Response(status,json=body)
        with httpx.Client(transport=httpx.MockTransport(request)) as client:
            return marketdata_options.collect_historical_chain(symbol='DVA',token='local-secret',cutoff=NOW,
                timeout_seconds=1,maximum_strikes=40,maximum_expiries=3,clock=lambda:captured,client=client,
                research_replay=research_replay)

    def test_late_research_keeps_original_quote_day_and_actual_capture(self):
        captured = datetime(2026,10,8,10,15,tzinfo=ET)
        result = self.collect(response_body(), captured=captured, research_replay=True)
        self.assertEqual(result['status'], 'collected')
        self.assertEqual(result['snapshot_session'], '2026-10-06')
        self.assertEqual(result['captured_at'], captured.isoformat())
        self.assertFalse(result['formal_cutoff_eligible'])
        self.assertTrue(result['post_cutoff_replay'])

    def test_strict_late_and_different_session_capture_are_rejected(self):
        with self.assertRaises(ValueError):
            self.collect(response_body(), captured=datetime(2026,10,8,10,15,tzinfo=ET))
        with self.assertRaises(ValueError):
            self.collect(response_body(), captured=datetime(2026,10,9,8,40,tzinfo=ET), research_replay=True)

    def test_late_research_still_rejects_future_quote_timestamp(self):
        body = response_body()
        body['updated'] = [int(datetime(2026,10,8,9,45,tzinfo=ET).timestamp())] * 2
        with self.assertRaises(ValueError):
            self.collect(body, captured=datetime(2026,10,8,10,15,tzinfo=ET), research_replay=True)

    def test_free_premarket_requests_correct_historical_day_and_keeps_field_times(self):
        result=self.collect(response_body())
        self.assertEqual(result['status'],'collected')
        self.assertEqual(result['snapshot_session'],'2026-10-06')
        self.assertEqual(result['contracts'][0]['quote_as_of'],'2026-10-06T16:00:00-04:00')
        self.assertIsNone(result['contracts'][0]['implied_volatility'])
        self.assertFalse(result['directional_flow_semantics'])
        self.assertEqual(result['atm_straddles'][0]['premium_mid'],8.0)
        self.assertNotIn('local-secret',str(result))

    def test_rejects_missing_time_wrong_symbol_and_future_snapshot(self):
        for field, value in [('updated',[None,None]),('underlying',['OTHER','DVA']),
                             ('updated',[STAMP+86400*2]*2),('bid',[99,4])]:
            body=response_body();body[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):
                self.collect(body)

    def test_permission_error_redacted_no_success(self):
        with self.assertRaisesRegex(ValueError,'权限') as caught:
            self.collect({'errmsg':'local-secret'},403)
        self.assertNotIn('local-secret',str(caught.exception))

    def test_local_token_never_enters_public_settings_or_keychain(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(ResearchSettingsStore,'_keyring',side_effect=AssertionError):
            store=ResearchSettingsStore(foundation.ResearchLabFoundationTests().paths(Path(directory)))
            store.set_marketdata_token('fixture-secret')
            reopened=ResearchSettingsStore(store.paths)
            self.assertEqual(reopened.get_marketdata_token(),'fixture-secret')
            self.assertTrue(reopened.public_settings()['marketdata_token_saved'])
            self.assertNotIn('fixture-secret',str(reopened.public_settings()))
