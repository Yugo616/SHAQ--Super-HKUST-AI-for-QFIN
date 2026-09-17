import tempfile
import unittest
from pathlib import Path

from shaq_daily_oracle.hashing import sha256_payload
from shaq_daily_oracle.minute_settlements import MinuteStore
from shaq_daily_oracle.virtual_accounts import AccountRules, replay_day


class EntryExceptionTests(unittest.TestCase):
    def test_explicit_exception_keeps_actual_minute_and_other_stock_unchanged(self):
        day = '2026-09-11'
        records = {s: [dict(timestamp=f'{day}T{t}:00-04:00', open=p, volume=100)
                       for t,p in values] for s,values in {
                           'AAA':[('09:32',100),('15:55',102)],
                           'BBB':[('09:31',50),('15:55',49)]}.items()}
        predictions = [dict(symbol='AAA',direction='bullish'),dict(symbol='BBB',direction='bearish')]
        with tempfile.TemporaryDirectory() as name:
            store=MinuteStore(Path(name))
            normal=store.observe(day,['AAA','BBB'],records,provider='yfinance',observed_at=f'{day}T16:10:00-04:00')
            before=replay_day(day,predictions,{},AccountRules(risk_fraction=None),minute=normal)
            self.assertEqual(before['status'],'unavailable')
            self.assertTrue(hasattr(store,'snapshot_with_entry_exception'), 'Explicit scoped exception API required')
            receipt={'trade_date':day,'batch_id':'batch','variant_key':'v','symbol':'AAA',
                     'original_entry_at_et':f'{day}T09:31:00-04:00',
                     'entry_at_et':f'{day}T09:32:00-04:00','reason':'User approved once',
                     'baseline_observation_hashes':normal['observation_hashes'],
                     'authorized_at_et':'2026-09-17T08:00:00-04:00'}
            receipt['exception_sha256']=sha256_payload(receipt)
            patched=store.snapshot_with_entry_exception(day,['AAA','BBB'],receipt)
            after=replay_day(day,predictions,{},AccountRules(risk_fraction=None),minute=patched)
            a,b=after['trades']
            self.assertEqual(a['entry_reference_at_et'],receipt['entry_at_et'])
            self.assertEqual(a['entry_exception']['exception_sha256'],receipt['exception_sha256'])
            self.assertGreater(a['quantity'],0)
            self.assertEqual(b,before['trades'][1])
            self.assertEqual(store.snapshot(day,['AAA','BBB']),normal)
            self.assertEqual(after['closing_positions'],{})
            self.assertTrue(all(o['reference_at_et']==receipt['entry_at_et'] for o in after['orders'] if o['symbol']=='AAA' and o['phase']=='open'))
            bad=dict(receipt,entry_at_et=f'{day}T09:33:00-04:00')
            with self.assertRaises(ValueError):store.snapshot_with_entry_exception(day,['AAA','BBB'],bad)
            missing=dict(receipt,entry_at_et=f'{day}T09:33:00-04:00')
            missing['exception_sha256']=sha256_payload({k:v for k,v in missing.items() if k!='exception_sha256'})
            with self.assertRaises(ValueError):store.snapshot_with_entry_exception(day,['AAA','BBB'],missing)
            with self.assertRaises(ValueError):store.snapshot_with_entry_exception('2026-09-14',['AAA','BBB'],receipt)
            records['AAA'].append(dict(timestamp=f'{day}T09:31:00-04:00',open=99,volume=100))
            store.observe(day,['AAA','BBB'],records,provider='yfinance',observed_at=f'{day}T16:20:00-04:00')
            retained=store.snapshot_with_entry_exception(day,['AAA','BBB'],receipt)
            self.assertEqual(retained['targets']['AAA']['entry']['timestamp'],receipt['entry_at_et'])
