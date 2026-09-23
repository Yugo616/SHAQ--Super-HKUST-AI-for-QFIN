"""Safe T-1 warmup orchestration around the existing daily cache."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from filelock import FileLock

from shaq_daily_oracle.data_providers import DataProfile
from shaq_daily_oracle.data_providers import DataProviderError
from shaq_daily_oracle.research_preparation import prepare_research_history


ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 22, 8, 10, tzinfo=ET)


class ResearchPreparationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        package = root / "package"
        config = package / "config"
        config.mkdir(parents=True)
        (config / "research-universe.csv").write_text(
            "instrument,company_name,gics_sector,gics_sub_industry,cik,known_from_utc\n"
            "AAA,AAA Inc,Technology,Software,1,2026-01-01T00:00:00Z\n"
            "BBB,BBB Inc,Technology,Software,2,2026-01-01T00:00:00Z\n",
            encoding="utf-8",
        )
        (config / "market-benchmarks.csv").write_text(
            "instrument,gics_sector\nXLK,Technology\nSPY,\n", encoding="utf-8",
        )
        (config / "public-data.json").write_text(
            json.dumps({"history_overlap_days": 3}), encoding="utf-8",
        )
        (config / "research-preparation.json").write_text(json.dumps({
            "lead_minutes": 20, "lookback_days": 400,
            "deadline_guard_seconds": 2, "lock_timeout_seconds": 0,
        }), encoding="utf-8")
        (config / "price-history.json").write_text(json.dumps({
            "lookback_calendar_days": 400,
        }), encoding="utf-8")
        self.paths = SimpleNamespace(package_root=package, research_root=root / "research")
        self.profile = DataProfile("test", "config/research-universe.csv",
                                   batch_size=2, request_timeout_seconds=30,
                                   yahoo_worker_timeout_seconds=900)

    def tearDown(self):
        self.temp.cleanup()

    def test_packaged_config_symlink_is_a_valid_universe(self):
        # PyInstaller macOS relocates data from Frameworks into sibling Resources.
        # Resolving the universe must not reject this shipped, versioned config.
        package = self.paths.package_root
        resources = package.parent / "Resources"
        resources.mkdir()
        (package / "config").rename(resources / "config")
        (package / "config").symlink_to(resources / "config", target_is_directory=True)
        with (patch("shaq_daily_oracle.research_preparation._wall_now", return_value=NOW),
              patch("shaq_daily_oracle.research_preparation.warm_previous_daily_bars",
                    return_value={"status": "deadline_exceeded", "completed_count": 0,
                                  "collected_count": 0, "no_data_count": 0,
                                  "pending_symbols": ["AAA", "BBB", "SPY", "XLK"]})):
            result = prepare_research_history(self.paths, self.profile, now=NOW,
                                             deadline_et=NOW + timedelta(seconds=12))
        self.assertEqual(result["requested_count"], 4)
        self.assertEqual(result["status"], "deadline_exceeded")

    def test_config_universe_symlink_cannot_escape_packaged_config(self):
        target = self.paths.package_root / "config/research-universe.csv"
        outside = self.paths.package_root.parent / "outside.csv"
        target.rename(outside)
        target.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "versioned package universe"):
            prepare_research_history(self.paths, self.profile, now=NOW,
                                     deadline_et=NOW + timedelta(seconds=12))

    def test_warms_only_versioned_prior_daily_cache_under_remaining_worker_budget(self):
        # Catches a current-session fetch, unbounded worker, wrong cache root or identity drift.
        calls = []
        created = []

        class FakeYahoo:
            def __init__(self, profile):
                self.profile = profile
                self.history_checkpoint_root = None
                created.append(self)

            def history(self, symbols, *, start, end, interval="1d", prepost=False):
                calls.append((list(symbols), start, end, interval, prepost,
                              self.profile.yahoo_worker_timeout_seconds,
                              self.profile.request_timeout_seconds,
                              self.history_checkpoint_root))
                return {symbol: [{"timestamp": "2026-09-21T00:00:00",
                                  "close": 100, "volume": 10}] for symbol in symbols}

        events = []
        with (patch("shaq_daily_oracle.research_preparation.YFinanceProvider", FakeYahoo, create=True),
              patch("shaq_daily_oracle.research_preparation._wall_now", return_value=NOW, create=True),
              patch("shaq_daily_oracle.research_preparation._monotonic", return_value=100, create=True)):
            result = prepare_research_history(
                self.paths, self.profile, now=NOW,
                deadline_et=NOW + timedelta(seconds=12), observer=events.append,
            )

        self.assertEqual(result["status"], "completed_with_gaps")
        self.assertEqual(result["requested_count"], 4)
        self.assertEqual(result["completed_count"], 4)
        self.assertEqual(result["complete_history_count"], 0)
        self.assertEqual(result["incomplete_symbols"], ["AAA", "BBB", "SPY", "XLK"])
        self.assertEqual(result["history_quality_by_symbol"]["AAA"]["reason"],
                         "leading_history_gap")
        self.assertTrue(result["history_quality_by_symbol"]["AAA"]["listing_start_unverified"])
        self.assertEqual(result["profile_sha256"], self.profile.identity())
        self.assertEqual([event["status"] for event in events if "symbol" in event],
                         ["collected"] * 4)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call[2:5] == (NOW.date(), "1d", False) for call in calls))
        self.assertTrue(all(0 < call[5] <= 10 and 0 < call[6] <= call[5] for call in calls))
        self.assertTrue(all(call[7] == self.paths.research_root / "cache/collection_requests"
                            / NOW.date().isoformat() / self.profile.history_identity()
                            for call in calls))
        self.assertEqual(len(list((self.paths.research_root / "cache/daily_bars"
                                   / self.profile.history_identity()).glob("*.json"))), 4)
        state = json.loads(Path(result["state_path"]).read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "completed_with_gaps")
        self.assertEqual(state["profile_sha256"], self.profile.identity())
        self.assertNotIn("secret", json.dumps(state).lower())

        # A second scheduler invocation retains the honest incomplete status
        # without hammering the same source again inside the 20-minute window.
        with (patch("shaq_daily_oracle.research_preparation.YFinanceProvider", FakeYahoo),
              patch("shaq_daily_oracle.research_preparation._wall_now", return_value=NOW),
              patch("shaq_daily_oracle.research_preparation._monotonic", return_value=100)):
            repeated = prepare_research_history(
                self.paths, self.profile, now=NOW,
                deadline_et=NOW + timedelta(seconds=12),
            )
        self.assertEqual(repeated["status"], "already_checked_incomplete")
        self.assertEqual(repeated["complete_history_count"], 0)
        self.assertEqual(len(calls), 2)

    def test_complete_nyse_session_window_is_reusable_without_new_request(self):
        config_path = self.paths.package_root / "config/price-history.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["lookback_calendar_days"] = 7
        config_path.write_text(json.dumps(config), encoding="utf-8")
        calls = []

        class FullHistoryYahoo:
            def __init__(self, profile):
                self.profile = profile
                self.history_checkpoint_root = None

            def history(self, symbols, *, start, end, interval="1d", prepost=False):
                calls.append(list(symbols))
                expected_days = ["2026-09-15", "2026-09-16", "2026-09-17",
                                 "2026-09-18", "2026-09-21"]
                return {symbol: [{"timestamp": day + "T00:00:00",
                                  "close": 100, "volume": 10} for day in expected_days]
                        for symbol in symbols}

        with (patch("shaq_daily_oracle.research_preparation.YFinanceProvider", FullHistoryYahoo),
              patch("shaq_daily_oracle.research_preparation._wall_now", return_value=NOW),
              patch("shaq_daily_oracle.research_preparation._monotonic", return_value=100)):
            first = prepare_research_history(self.paths, self.profile, now=NOW,
                deadline_et=NOW + timedelta(seconds=12))
            second = prepare_research_history(self.paths, self.profile, now=NOW,
                deadline_et=NOW + timedelta(seconds=12))
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["complete_history_count"], 4)
        self.assertEqual(first["incomplete_symbols"], [])
        self.assertEqual(second["status"], "already_completed")
        self.assertEqual(second["complete_history_count"], 4)
        self.assertEqual(len(calls), 2)

    def test_missing_t_minus_one_is_incomplete_even_with_other_sessions(self):
        config_path = self.paths.package_root / "config/price-history.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["lookback_calendar_days"] = 7
        config_path.write_text(json.dumps(config), encoding="utf-8")

        class MissingLastSessionYahoo:
            def __init__(self, profile):
                self.profile = profile
                self.history_checkpoint_root = None

            def history(self, symbols, *, start, end, interval="1d", prepost=False):
                return {symbol: [{"timestamp": day + "T00:00:00",
                                  "close": 100, "volume": 10}
                                 for day in ("2026-09-15", "2026-09-16",
                                             "2026-09-17", "2026-09-18")]
                        for symbol in symbols}

        with (patch("shaq_daily_oracle.research_preparation.YFinanceProvider", MissingLastSessionYahoo),
              patch("shaq_daily_oracle.research_preparation._wall_now", return_value=NOW),
              patch("shaq_daily_oracle.research_preparation._monotonic", return_value=100)):
            result = prepare_research_history(self.paths, self.profile, now=NOW,
                deadline_et=NOW + timedelta(seconds=12))
        self.assertEqual(result["status"], "completed_with_gaps")
        self.assertEqual(result["complete_history_count"], 0)
        self.assertEqual(result["history_quality_by_symbol"]["AAA"]["reason"],
                         "missing_previous_session")

    def test_empty_response_is_checked_but_not_complete_history(self):
        calls = []

        class EmptyYahoo:
            def __init__(self, profile):
                self.history_checkpoint_root = None

            def history(self, symbols, *, start, end, interval="1d", prepost=False):
                calls.append(list(symbols))
                return {symbol: [] for symbol in symbols}

        with (patch("shaq_daily_oracle.research_preparation.YFinanceProvider", EmptyYahoo),
              patch("shaq_daily_oracle.research_preparation._wall_now", return_value=NOW),
              patch("shaq_daily_oracle.research_preparation._monotonic", return_value=100)):
            first = prepare_research_history(self.paths, self.profile, now=NOW,
                deadline_et=NOW + timedelta(seconds=12))
            second = prepare_research_history(self.paths, self.profile, now=NOW,
                deadline_et=NOW + timedelta(seconds=12))
        self.assertEqual(first["status"], "completed_with_gaps")
        self.assertEqual(first["completed_count"], 4)
        self.assertEqual(first["no_data_count"], 4)
        self.assertEqual(first["complete_history_count"], 0)
        self.assertEqual(first["history_quality_by_symbol"]["AAA"]["reason"],
                         "no_verified_cache")
        self.assertEqual(second["status"], "already_checked_incomplete")
        self.assertEqual(len(calls), 2)

    def test_no_writes_or_provider_on_closed_session_or_past_deadline(self):
        # Catches an off-session/read-only invocation starting a warmup.
        with patch("shaq_daily_oracle.research_preparation.YFinanceProvider", create=True) as provider:
            closed = prepare_research_history(self.paths, self.profile,
                now=NOW.replace(day=20), deadline_et=NOW.replace(day=20) + timedelta(minutes=5))
            late = prepare_research_history(self.paths, self.profile,
                now=NOW, deadline_et=NOW)
        self.assertEqual(closed["status"], "not_applicable")
        self.assertEqual(late["status"], "not_applicable")
        provider.assert_not_called()
        self.assertFalse(self.paths.research_root.exists())

    def test_lock_conflict_returns_without_starting_provider(self):
        # Catches GUI and scheduler racing the same preparation session.
        state_root = self.paths.research_root / "preparation"
        state_root.mkdir(parents=True)
        state_path = state_root / f"{NOW.date().isoformat()}-{self.profile.identity()}.json"
        with (FileLock(str(state_path) + ".lock", timeout=0),
              patch("shaq_daily_oracle.research_preparation.YFinanceProvider") as provider):
            result = prepare_research_history(
                self.paths, self.profile, now=NOW,
                deadline_et=NOW + timedelta(seconds=12),
            )
        self.assertEqual(result["status"], "already_running")
        provider.assert_not_called()
        self.assertFalse(state_path.exists())

    def test_guard_window_expires_without_starting_a_network_worker(self):
        # Catches describing a deliberate deadline stop as a provider failure.
        with (patch("shaq_daily_oracle.research_preparation.YFinanceProvider") as provider,
              patch("shaq_daily_oracle.research_preparation._wall_now", return_value=NOW),
              patch("shaq_daily_oracle.research_preparation._monotonic", return_value=100)):
            result = prepare_research_history(
                self.paths, self.profile, now=NOW,
                deadline_et=NOW + timedelta(seconds=1),
            )
        self.assertEqual(result["status"], "deadline_exceeded")
        provider.assert_not_called()

    def test_retry_only_missing_cached_symbols_after_partial_failure(self):
        # Catches wasting the bounded window by re-downloading finished batches.
        config_path = self.paths.package_root / "config/price-history.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["lookback_calendar_days"] = 7
        config_path.write_text(json.dumps(config), encoding="utf-8")
        calls = []
        failure = [True]

        class SometimesFailingYahoo:
            def __init__(self, profile):
                self.profile = profile
                self.history_checkpoint_root = None

            def history(self, symbols, *, start, end, interval="1d", prepost=False):
                calls.append(list(symbols))
                if symbols == ["SPY", "XLK"] and failure[0]:
                    raise DataProviderError("temporary", diagnostic={"kind": "connection_error"})
                expected_days = ["2026-09-15", "2026-09-16", "2026-09-17",
                                 "2026-09-18", "2026-09-21"]
                return {symbol: [{"timestamp": day + "T00:00:00",
                                  "close": 100, "volume": 10} for day in expected_days]
                        for symbol in symbols}

        with (patch("shaq_daily_oracle.research_preparation.YFinanceProvider", SometimesFailingYahoo),
              patch("shaq_daily_oracle.research_preparation._wall_now", return_value=NOW),
              patch("shaq_daily_oracle.research_preparation._monotonic", return_value=100)):
            first = prepare_research_history(
                self.paths, self.profile, now=NOW,
                deadline_et=NOW + timedelta(seconds=12),
            )
            failure[0] = False
            second = prepare_research_history(
                self.paths, self.profile, now=NOW,
                deadline_et=NOW + timedelta(seconds=12),
            )
        self.assertEqual(first["status"], "provider_error")
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["completed_count"], 4)
        self.assertEqual(calls, [["AAA", "BBB"], ["SPY", "XLK"], ["SPY", "XLK"]])


if __name__ == "__main__":
    unittest.main()
