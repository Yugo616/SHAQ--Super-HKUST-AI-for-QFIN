"""Late acquisition stays useful for research, never becomes an on-time forecast."""
import json
import tempfile
import unittest
from datetime import datetime
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
import test_alpaca_bars as bars_fixture
import test_research_collection as collection_fixture
import test_review_view as ui_fixture
import test_research_lab_foundation as foundation
from shaq_daily_oracle.research_settings import ResearchSettingsStore, SettingsError
from shaq_daily_oracle import public_data
from shaq_daily_oracle.research_collection import collect_research_evidence
from shaq_daily_oracle.research_batch import _tasks_for_domain

ET = ZoneInfo('America/New_York')


class LateDataTests(unittest.TestCase):
    def test_late_quote_context_reaches_research_with_actual_capture(self):
        late=datetime(2026,9,4,9,20,tzinfo=ET)
        response={'provider':'Alpaca Market Data','feed':'sip',
            'source_uri':'https://data.alpaca.markets/v2/stocks/quotes',
            'capture_completed_at':late.isoformat(),
            'symbols':{'AAPL':{'status':'computed','formal_cutoff_eligible':False,
                'post_cutoff_replay':True,'calculation':{'ofi_quote_size':4,'native_trade_aggressor':False},
                'raw_pages':[{'body_utf8':'{"quotes":{"AAPL":[]}}',
                    'request_uri':'https://data.alpaca.markets/v2/stocks/quotes'}]}}}
        with tempfile.TemporaryDirectory() as tmp, patch(
                'shaq_daily_oracle.alpaca_iex_quotes.collect_quote_pressure',return_value=response):
            evidence=collect_research_evidence(root=Path(tmp)/'evidence',package_root=collection_fixture.PACKAGE_ROOT,
                profile=replace(collection_fixture.ResearchCollectionTests().profile(),alpaca_orderflow_enabled=True),
                alpaca_credentials=('key','secret'),sec_identity='Research test@example.edu',
                observed_at=late,collection_clock=lambda:late,market_provider=collection_fixture.FakeMarket(),
                metadata_provider=collection_fixture.FakeMetadata(),event_provider=collection_fixture.FakeEvents())
            task=next(t for t in _tasks_for_domain(evidence,'capital') if t['symbol']=='AAPL')
            row=next(r for r in task['evidence'] if r['provider']=='alpaca-sip')
            self.assertEqual(row['captured_at'],late.isoformat())
            self.assertFalse(row['content']['formal_cutoff_eligible'])
            self.assertFalse(row['content']['calculation']['native_trade_aggressor'])
            self.assertEqual(evidence.manifest['cutoff_status'],'late_research_only')

    def test_auth_failure_not_retried_by_public_fallback(self):
        config={'timeout_seconds':1,'retry_backoff_seconds':0,'sources':{
            'sample':{'url':'https://example.test/data','kind':'csv','enabled':True}}}
        response=httpx.Response(403,request=httpx.Request('GET','https://example.test/data'))
        with patch('curl_cffi.requests.Session.get',return_value=response) as fetch:
            packet=public_data.collect_public_context(config,cutoff=datetime(2030,1,1,tzinfo=ET))[0]
        self.assertEqual(packet['status'],'provider_error')
        self.assertNotIn('data',packet)
        self.assertEqual(fetch.call_count,1)

    def test_network_exhaustion_stops_after_bounded_transports(self):
        config={'timeout_seconds':1,'retry_backoff_seconds':0,'sources':{
            'sample':{'url':'https://example.test/data','kind':'csv','enabled':True}}}
        with patch('curl_cffi.requests.Session.get',side_effect=ConnectionError()) as first, patch(
                'httpx.Client.stream',side_effect=httpx.ConnectError('offline')) as fallback:
            packet=public_data.collect_public_context(config,cutoff=datetime(2030,1,1,tzinfo=ET))[0]
        self.assertEqual(packet['status'],'provider_error')
        self.assertEqual(first.call_count,2)
        self.assertEqual(fallback.call_count,1)

    def test_late_research_bars_keep_original_market_window_and_real_capture(self):
        late = datetime(2026, 10, 2, 9, 20, tzinfo=ET)
        def reply(request):
            self.assertEqual(request.url.params['end'], '2026-10-02T08:34:00-04:00')
            return httpx.Response(200, json={'bars': {'APP': [
                bars_fixture.bar('2026-10-02T12:01:00Z', 25),
                bars_fixture.bar('2026-10-02T13:01:00Z', 99)]}, 'next_page_token': None})
        with httpx.Client(transport=httpx.MockTransport(reply)) as client:
            result = bars_fixture.AlpacaBarsTests().request(client,
                observed_at=late, clock=lambda: late, research_replay=True)
        self.assertEqual(result['symbols']['APP']['observed_volume'], 25)
        self.assertFalse(result['metadata']['formal_cutoff_eligible'])
        self.assertEqual(result['metadata']['captured_at_et'], late.isoformat())

    def test_same_day_late_occ_reaches_domain_but_cannot_become_on_time(self):
        late = datetime(2026, 9, 4, 9, 20, tzinfo=ET)
        body = (b'Series Search Results for AAPL\n'
            b'ProductSymbol\tyear\tMonth\tDay\tInteger\tDec\tC/P\tCall\tPut\tPosition Limit\n'
            b'AAPL\t\t2026\t09\t18\t200\t000\tC P\t100\t200\t250000\n')
        with tempfile.TemporaryDirectory() as tmp, patch(
                'shaq_daily_oracle.occ_options.fetch_occ_series_search',
                side_effect=lambda s, **kw: body.replace(b'AAPL', s.encode())):
            evidence = collect_research_evidence(root=Path(tmp)/'evidence',
                package_root=collection_fixture.PACKAGE_ROOT,
                profile=replace(collection_fixture.ResearchCollectionTests().profile(),
                                occ_open_interest_enabled=True),
                observed_at=late, collection_clock=lambda: late,
                sec_identity='Research test@example.edu',
                market_provider=collection_fixture.FakeMarket(),
                metadata_provider=collection_fixture.FakeMetadata(),
                event_provider=collection_fixture.FakeEvents())
            task = next(t for t in _tasks_for_domain(evidence,'derivatives') if t['symbol']=='AAPL')
            rows = [r['content'] for r in task['evidence'] if r['provider']=='occ']
            self.assertEqual(rows[0]['total_call_open_interest'], 100)
            self.assertEqual(evidence.manifest['cutoff_status'],'late_research_only')
            self.assertEqual(rows[0]['captured_at'],late.isoformat())

    def test_public_network_failure_retries_same_source_and_preserves_bytes(self):
        payload = b'DATE,CLOSE\n01/02/2025,15.0\n'
        response = httpx.Response(200,content=payload,request=httpx.Request('GET','https://example.test/data'))
        config={'timeout_seconds':1,'retry_backoff_seconds':0,'sources':{
            'vix':{'url':'https://example.test/data','kind':'csv','enabled':True}}}
        with patch('curl_cffi.requests.Session.get',side_effect=[ConnectionError(),response]):
            result=public_data.collect_public_context(config,
                cutoff=datetime(2030,1,1,tzinfo=ET))[0]
        self.assertEqual(result['status'],'collected')
        self.assertEqual(result['raw'],payload)

    def test_public_late_research_keeps_capture_status_and_past_observations(self):
        payload=b'DATE,CLOSE\n09/03/2026,15.0\n09/04/2026,99.0\n'
        response=httpx.Response(200,content=payload,request=httpx.Request('GET','https://example.test/data'))
        config={'timeout_seconds':1,'sources':{'vix':{'url':'https://example.test/data','kind':'csv','enabled':True}}}
        late=datetime(2026,9,4,9,20,tzinfo=ET)
        with patch('curl_cffi.requests.Session.get',return_value=response):
            result=public_data.collect_public_context(config,
                cutoff=datetime(2026,9,4,8,50,tzinfo=ET),research_replay=True,clock=lambda:late)[0]
        self.assertEqual(result['status'],'collected')
        self.assertFalse(result['formal_cutoff_eligible'])
        self.assertEqual(result['data'],[{'DATE':'09/03/2026','CLOSE':'15.0'}])


class SetupDisplayTests(unittest.TestCase):
    def test_saved_identity_is_not_a_missing_field_after_profile_recheck(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=ResearchSettingsStore(foundation.ResearchLabFoundationTests().paths(Path(tmp)))
            store.save_research_setup({'sec_identity':'Research test@example.edu'})
            store.save_research_setup({'data_profile':{'occ_open_interest_enabled':True}})
            public=store.public_settings()
            self.assertEqual(public['sec_identity'],'Research test@example.edu')
            self.assertNotIn('sec_identity',public['setup_required_fields'])
            self.assertFalse(public['setup_complete'])

    def test_blank_identity_submission_does_not_erase_saved_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=ResearchSettingsStore(foundation.ResearchLabFoundationTests().paths(Path(tmp)))
            store.save_research_setup({'sec_identity':'Research test@example.edu'})
            with self.assertRaises(SettingsError):
                store.save_research_setup({'sec_identity':'   '})
            self.assertEqual(store.load()['sec_identity'],'Research test@example.edu')

    def test_optional_data_recheck_does_not_reopen_first_setup(self):
        value=ui_fixture.ReviewViewTests().bundle(r'''
let opens=0;
nodes['#setup']={classList:{remove(){opens++}}};
vm.runInContext(`state.data={operator_mode:{safety_ready:false},settings:{
 setup_complete:false,setup_required_fields:[],sec_identity:'Research test@example.edu'},result_refresh:{}};
 renderRefreshControls=()=>{};render(false,false);render(false,false)`,ctx);
console.log(JSON.stringify({opens,status:nodes['#service-text'].textContent}));
''')
        self.assertEqual(value['opens'],0)
        self.assertNotIn('首次',value['status'])
