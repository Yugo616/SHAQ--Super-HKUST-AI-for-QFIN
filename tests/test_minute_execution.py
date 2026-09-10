"""Independent literal expectations for the synthetic Zipline execution probe."""
import pandas as pd
import dataclasses
from datetime import time, timedelta
import importlib
import unittest

try:
    probe = importlib.import_module("shaq_daily_oracle.minute_execution")
except ModuleNotFoundError as exc:
    if exc.name != "shaq_daily_oracle.minute_execution":
        raise
    probe = None


def synthetic_fixture(session="2026-09-09", prices=None, rules=None, signals=None):
    """Deterministic synthetic unadjusted one-minute OHLCV, not market evidence."""
    rules = rules or probe.Rules()
    if prices is None:
        prices = {"LONG": (100., 110.), "SHORT": (100., 90.)}
        signals = signals if signals is not None else (probe.Signal("LONG", 1), probe.Signal("SHORT", -1))
    elif signals is None:
        raise ValueError("Custom fixture prices require explicit frozen signals")
    minutes, _, exit_ = probe.execution_schedule(session, rules)
    signals = tuple(signals)
    bars = {}
    for symbol, (start, end) in prices.items():
        for minute in minutes:
            price = end if minute >= exit_ else start
            bars[symbol, minute] = probe.Bar(price, price, price, price, 10000.)
    return probe.SessionInput(session, signals, bars)



class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(probe, "Zipline execution adapter has not been implemented")

    def fixture(self, day="2026-09-09", prices=None, directions=None):
        if prices is None:
            return synthetic_fixture(day)
        directions = directions or {symbol: 1 for symbol in prices}
        return synthetic_fixture(day, prices, signals=tuple(
            probe.Signal(symbol, direction) for symbol, direction in directions.items()))

    def test_custom_fixture_cannot_infer_directions_from_future_prices(self):
        with self.assertRaises(ValueError):
            synthetic_fixture(prices={"UNKNOWN": (100., 110.)})

    def test_real_engine_long_short_and_independent_reconciliation(self):
        result = probe.run_session(self.fixture())
        self.assertEqual(result["engine"]["version"], "3.1.1")
        self.assertEqual(result["shares"], {"LONG": 9, "SHORT": 9})
        for key, expected in {"gross_pnl": 180., "commissions": 1.799955,
                              "slippage_cost": 1.8, "net_pnl": 176.400045,
                              "final_cash": 10176.400045}.items():
            self.assertAlmostEqual(result[key], expected, places=8)
        self.assertAlmostEqual(result["zero_cost"]["final_cash"], 10180., places=8)
        self.assertEqual(result["zero_cost"]["shares"], result["shares"])
        self.assertEqual(result["positions"], {})
        self.assertTrue(result["settled"])
        self.assertTrue(result["reconciled"])
        self.assertEqual(len(result["fills"]), 4)
        self.assertEqual(result["fills"][0]["bar_start"], "2026-09-09T13:31:00+00:00")
        self.assertEqual(result["fills"][-1]["bar_start"], "2026-09-09T19:55:00+00:00")
        self.assertEqual(result["minutes_processed"], 390)

    def test_correct_direction_can_lose_after_costs(self):
        fixture = self.fixture(prices={"LONG": (100., 100.05)})
        result = probe.run_session(fixture)
        self.assertAlmostEqual(result["gross_pnl"], .45, places=8)
        self.assertAlmostEqual(result["net_pnl"], -1.3504498875, places=8)

    def test_flat_price_loses_only_costs(self):
        result = probe.run_session(self.fixture(prices={"FLAT": (100., 100.)}))
        self.assertEqual(result["gross_pnl"], 0.)
        self.assertAlmostEqual(result["commissions"], .9, places=8)
        self.assertAlmostEqual(result["slippage_cost"], .9, places=8)
        self.assertAlmostEqual(result["net_pnl"], -1.8, places=8)

    def test_multiple_longs_and_shorts(self):
        fixture = self.fixture(prices={"L1": (100., 110.), "L2": (100., 110.),
                                       "S1": (100., 90.), "S2": (100., 90.)},
                               directions={"L1": 1, "L2": 1, "S1": -1, "S2": -1})
        result = probe.run_session(fixture)
        self.assertEqual(len(result["fills"]), 8)
        self.assertEqual(result["shares"], {"L1": 9, "L2": 9, "S1": 9, "S2": 9})
        self.assertAlmostEqual(result["final_cash"], 10352.80009, places=8)

    def test_malformed_directions_and_duplicate_symbols_are_rejected(self):
        fixture = self.fixture()
        for signals in ((probe.Signal("BAD", 0),), (probe.Signal("BAD", 2),),
                        (probe.Signal("DUP", 1), probe.Signal("DUP", -1))):
            with self.subTest(signals=signals), self.assertRaises(ValueError):
                probe.run_session(dataclasses.replace(fixture, signals=signals))

    def test_equal_budget_cash_constraints_and_no_short_financing(self):
        for initial, expected in [(1000., 4), (200., 0), (0., 0)]:
            with self.subTest(initial=initial):
                result = probe.run_session(self.fixture(), probe.Rules(initial_cash=initial))
                self.assertEqual(result["shares"], {"LONG": expected, "SHORT": expected})
                self.assertLessEqual(result["reserved_collateral"], initial)
                self.assertEqual(result["positions"], {})

    def test_empty_signals_is_settled_no_trade(self):
        fixture = dataclasses.replace(self.fixture(), signals=())
        result = probe.run_session(fixture)
        self.assertEqual(result["fills"], [])
        self.assertEqual(result["final_cash"], 10000.)
        self.assertTrue(result["settled"])

    def test_missing_and_nonpositive_entry_never_fill(self):
        for mode in ("missing", "zero", "negative"):
            with self.subTest(mode=mode):
                fixture = self.fixture(prices={"LONG": (100., 110.)})
                bars = dict(fixture.bars)
                target = next(k for k in bars if k[0] == "LONG" and k[1].strftime("%H:%M") == "13:31")
                if mode == "missing":
                    del bars[target]
                else:
                    bars[target] = dataclasses.replace(bars[target], volume=0 if mode == "zero" else -1)
                result = probe.run_session(dataclasses.replace(fixture, bars=bars))
                self.assertEqual(result["fills"], [])
                self.assertEqual(result["final_cash"], 10000.)
                self.assertTrue(result["settled"])
                self.assertEqual(result['shares'], {'LONG': 0})
                self.assertEqual(result['reserved_collateral'], 0.)

    def test_missing_and_zero_exit_remain_unresolved_with_real_position(self):
        for mode in ("missing", "zero"):
            with self.subTest(mode=mode):
                fixture = self.fixture(prices={"LONG": (100., 110.)})
                bars = dict(fixture.bars)
                target = next(k for k in bars if k[1].strftime("%H:%M") == "19:55")
                if mode == "missing":
                    del bars[target]
                else:
                    bars[target] = dataclasses.replace(bars[target], volume=0)
                result = probe.run_session(dataclasses.replace(fixture, bars=bars))
                self.assertEqual(len(result["fills"]), 1)
                self.assertEqual(result["positions"], {"LONG": 9})
                self.assertFalse(result["settled"])
                self.assertIsNone(result["net_pnl"])
                self.assertAlmostEqual(result["final_cash"], 9099.099775, places=8)

    def test_exchange_schedule_early_close_and_dst(self):
        cases = [("2026-11-27", "14:31", "17:55", 210),
                 ("2026-03-06", "14:31", "20:55", 390),
                 ("2026-03-09", "13:31", "19:55", 390)]
        for day, entry, exit_, minutes in cases:
            with self.subTest(day=day):
                result = probe.run_session(self.fixture(day))
                self.assertEqual(result["fills"][0]["bar_start"][11:16], entry)
                self.assertEqual(result["fills"][-1]["bar_start"][11:16], exit_)
                self.assertEqual(result["minutes_processed"], minutes)

    def test_future_fields_do_not_change_sizing_or_execution(self):
        fixture = self.fixture()
        original = probe.run_session(fixture)
        changed = {key: dataclasses.replace(bar, high=1e7, low=.01, close=98765., volume=99999999)
                   for key, bar in fixture.bars.items()}
        perturbed = probe.run_session(dataclasses.replace(fixture, bars=changed))
        for key in ("shares", "fills", "final_cash", "net_pnl"):
            self.assertEqual(original[key], perturbed[key])
        self.assertTrue(perturbed["directions_unchanged"])

    def test_future_opens_except_contractual_exit_do_not_change_results(self):
        fixture = self.fixture()
        baseline = probe.run_session(fixture)
        changed = {key: dataclasses.replace(bar, open=123456.)
                   if key[1].strftime("%H:%M") not in ("13:31", "19:55") else bar
                   for key, bar in fixture.bars.items()}
        result = probe.run_session(dataclasses.replace(fixture, bars=changed))
        for key in ("shares", "fills", "final_cash"):
            self.assertEqual(result[key], baseline[key])

    def test_directions_are_frozen_and_original_hash_preserved(self):
        fixture = self.fixture()
        original_signals = fixture.signals
        with self.assertRaises(dataclasses.FrozenInstanceError):
            fixture.signals[0].direction = -1
        report = probe.run_session(fixture)
        self.assertEqual(fixture.signals, original_signals)
        self.assertTrue(report["directions_unchanged"])

    def test_invalid_rules_are_rejected(self):
        for rules in (probe.Rules(initial_cash=-1), probe.Rules(slippage=1),
                      probe.Rules(commission=float("nan")), probe.Rules(ticket_budget=-1)):
            with self.subTest(rules=rules), self.assertRaises(ValueError):
                probe.run_session(self.fixture(), rules)

    def test_nonminute_execution_targets_are_rejected_not_silently_skipped(self):
        for rules in (probe.Rules(entry_time=time(9, 31, 30)),
                      probe.Rules(entry_time=time(9, 31, microsecond=1)),
                      probe.Rules(exit_minutes_before_close=5.5)):
            with self.subTest(rules=rules), self.assertRaises(ValueError):
                probe.run_session(self.fixture(), rules)

    def test_bar_timestamps_must_be_aware_and_on_session_minute_grid(self):
        fixture = self.fixture()
        symbol, timestamp = next(iter(fixture.bars))
        for invalid in (timestamp.tz_localize(None),
                        timestamp + timedelta(seconds=30),
                        timestamp + timedelta(microseconds=1),
                        timestamp - timedelta(minutes=1)):
            with self.subTest(timestamp=invalid):
                bars = dict(fixture.bars)
                bar = bars.pop((symbol, timestamp))
                bars[symbol, invalid] = bar
                with self.assertRaises(ValueError):
                    probe.run_session(dataclasses.replace(fixture, bars=bars))

    def test_symbol_permutation_has_no_allocation_bias(self):
        fixture = self.fixture()
        rules = probe.Rules(initial_cash=1000.)
        original = probe.run_session(fixture, rules)
        changed = dataclasses.replace(fixture, signals=tuple(reversed(fixture.signals)),
                                      bars=dict(reversed(list(fixture.bars.items()))))
        permuted = probe.run_session(changed, rules)
        for key in ("shares", "fills", "reserved_collateral", "final_cash"):
            self.assertEqual(original[key], permuted[key])


if __name__ == "__main__":
    unittest.main()
