from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest import mock

from shaq_daily_oracle.data_providers import DataProfile
from shaq_daily_oracle.model_backends import ModelProfile
from shaq_daily_oracle.research_batch import (
    ResearchBatchRunner,
    VariantSelection,
    freeze_evidence_bundle,
    load_frozen_evidence,
)
from shaq_daily_oracle.research_dashboard import ResearchDashboardIndex
from shaq_daily_oracle import research_labels
from shaq_daily_oracle.research_labels import refresh_research_labels
from shaq_daily_oracle.skill_versions import LocalSkillRegistry
from test_research_batch import FakeModel


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


class LabelMarket:
    def history(self, symbols, *, start, end, interval="1d", prepost=False):
        return {
            symbol: [{
                "timestamp": "2026-09-04T16:00:00-04:00",
                "open": 100.0, "close": 102.0, "adj_close": 999.0,
            }]
            for symbol in symbols
        }


class ResearchLabelTests(unittest.TestCase):
    def recompute(self, value):
        self.assertTrue(hasattr(research_labels, "recompute_label"),
                        "saved observations need a canonical status recomputation")
        return research_labels.recompute_label(value)

    def observation(self, at, opening=100.0, closing=102.0):
        return {
            "observed_at_et": at,
            "provider": "yfinance",
            "official_unadjusted_open": opening,
            "official_unadjusted_close": closing,
            "open_to_close_return": closing / opening - 1,
            "actual_direction": "bullish" if closing > opening else "bearish",
            "observation_sha256": at,
        }

    def test_confirmed_label_survives_same_day_unchanged_refresh(self):
        observations = [
            self.observation("2026-09-04T16:05:00-04:00"),
            self.observation("2026-09-08T09:00:00-04:00"),
            self.observation("2026-09-08T10:00:00-04:00"),
        ]
        label = self.recompute({"observations": observations})
        self.assertEqual(label["status"], "final")
        self.assertTrue(label["confirmed_by_independent_reobservation"])
        self.assertEqual([row["observed_at_et"] for row in label["observations"]], [
            "2026-09-04T16:05:00-04:00", "2026-09-08T09:00:00-04:00",
            "2026-09-08T10:00:00-04:00",
        ])

    def test_revision_is_retained_and_requires_new_trading_day_confirmation(self):
        original = [
            self.observation("2026-09-04T16:05:00-04:00"),
            self.observation("2026-09-08T09:00:00-04:00"),
        ]
        revised = self.recompute({"observations": original + [
            self.observation("2026-09-08T10:00:00-04:00", closing=101.0),
        ]})
        self.assertEqual(revised["status"], "provisional")
        self.assertEqual(len(revised["corrections"]), 1)
        self.assertEqual(revised["earliest_eligible_confirmation_trading_day"], "2026-09-09")
        reconfirmed = self.recompute({
            "observations": revised["observations"] + [
                self.observation("2026-09-09T09:00:00-04:00", closing=101.0),
            ],
            "corrections": revised["corrections"],
        })
        self.assertEqual(reconfirmed["status"], "final")
        self.assertEqual(len(reconfirmed["corrections"]), 1)

    def test_legacy_unsorted_observations_are_repaired_without_fabrication(self):
        later = self.observation("2026-09-08T09:00:00-04:00")
        earlier = self.observation("2026-09-04T16:05:00-04:00")
        label = self.recompute({"status": "provisional", "observations": [later, earlier]})
        self.assertEqual(label["status"], "final")
        self.assertEqual(label["observations"], [earlier, later])
        self.assertEqual(len(label["observations"]), 2)

    def test_saved_observations_repair_even_when_current_provider_is_offline(self):
        class OfflineMarket:
            def history(self, *args, **kwargs):
                raise OSError("offline")

        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "research"
            batch_id = self.setup_batch(root)
            path = root / "batches" / batch_id / "labels.json"
            observations = [
                self.observation("2026-09-08T09:00:00-04:00"),
                self.observation("2026-09-04T16:05:00-04:00"),
            ]
            path.write_text(json.dumps({"schema_version": 1, "labels": {
                "AAPL": {"status": "provisional", "observations": observations},
            }}), encoding="utf-8")
            receipt = refresh_research_labels(
                research_root=root, batches_root=root / "batches",
                profile=DataProfile(profile_id="test", universe_file="unused.csv"),
                observed_at=datetime(2026, 9, 9, 9, tzinfo=ZoneInfo("America/New_York")),
                market_provider=OfflineMarket(),
            )
            saved = json.loads(path.read_text(encoding="utf-8"))["labels"]["AAPL"]
        self.assertEqual(saved["status"], "final")
        self.assertEqual(len(saved["observations"]), 2)
        self.assertEqual(receipt["refreshed_batches"], [])
        self.assertEqual(receipt["failures"][0]["error_type"], "OSError")

    def test_malformed_label_batch_isolated_from_later_healthy_batch(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "research"
            healthy_id = self.setup_batch(root)
            healthy = root / "batches" / healthy_id
            malformed = root / "batches" / "LAB-2026-09-03-malformed"
            malformed.mkdir()
            (malformed / "batch_manifest.json").write_bytes(
                (healthy / "batch_manifest.json").read_bytes())
            (malformed / "labels.json").write_text("{broken", encoding="utf-8")
            result = refresh_research_labels(
                research_root=root, batches_root=root / "batches",
                profile=DataProfile(profile_id="test", universe_file="unused.csv"),
                observed_at=datetime(2026, 9, 8, 9, tzinfo=ZoneInfo("America/New_York")),
                market_provider=LabelMarket(),
            )
        self.assertEqual(result["refreshed_batches"], [healthy_id])
        self.assertEqual(result["failures"][0]["batch_id"], malformed.name)
        self.assertEqual(result["failures"][0]["error_type"], "JSONDecodeError")

    def test_missing_expected_daily_bar_is_a_batch_failure(self):
        class MissingMarket:
            def history(self, *args, **kwargs):
                return {}

        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "research"
            batch_id = self.setup_batch(root)
            result = refresh_research_labels(
                research_root=root, batches_root=root / "batches",
                profile=DataProfile(profile_id="test", universe_file="unused.csv"),
                observed_at=datetime(2026, 9, 8, 9, tzinfo=ZoneInfo("America/New_York")),
                market_provider=MissingMarket(),
            )
        self.assertEqual(result["refreshed_batches"], [])
        self.assertEqual(result["failures"][0]["batch_id"], batch_id)
        self.assertEqual(result["failures"][0]["missing_symbols"], ["AAPL"])
    def test_openbb_label_refresh_receives_explicit_credential(self):
        profile = DataProfile(
            profile_id="openbb-test",
            universe_file="unused.csv",
            market_provider="openbb-rest",
            openbb_base_url="https://openbb.test",
        )
        with tempfile.TemporaryDirectory() as name, mock.patch(
            "shaq_daily_oracle.research_labels.OpenBBProviderAdapter"
        ) as adapter:
            root = Path(name)
            refresh_research_labels(
                research_root=root,
                batches_root=root / "batches",
                profile=profile,
                openbb_api_key="credential-store-value",
            )
        self.assertEqual(adapter.call_args.kwargs["api_key"], "credential-store-value")

    def setup_batch(self, root: Path):
        staging = root / "staging/evidence"
        evidence = freeze_evidence_bundle(
            root=staging,
            as_of_et="2026-09-04T08:49:00-04:00",
            scheduled_cutoff_et="2026-09-04T08:50:00-04:00",
            cutoff_status="on_time",
            candidates=[{"symbol": "AAPL", "gics_sector": "Information Technology"}],
            records=[
                {
                    "evidence_id": "ev_market", "domain": "market", "provider": "test",
                    "source_uri": "https://example.test/market",
                    "captured_at": "2026-09-04T08:49:00-04:00",
                    "raw_file_path": "raw/market.json", "scope_symbols": ["*"],
                    "consumer_domains": ["market", "price_volume"],
                    "root_component_type": "market_context",
                },
                {
                    "evidence_id": "ev_price", "domain": "price_volume", "provider": "test",
                    "source_uri": "https://example.test/aapl",
                    "captured_at": "2026-09-04T08:49:00-04:00",
                    "raw_file_path": "raw/aapl.json", "scope_symbols": ["AAPL"],
                    "consumer_domains": ["market", "price_volume"],
                    "root_component_type": "stock_price_volume",
                },
            ],
            files={"raw/market.json": b'{"market":1}', "raw/aapl.json": b'{"stock":1}'},
            provider_manifest={"profile_id": "test", "manifest_sha256": "m" * 64},
        )
        target = root / "evidence" / evidence.manifest["evidence_hash"]
        target.parent.mkdir(parents=True)
        staging.replace(target)
        evidence = load_frozen_evidence(target)
        registry = LocalSkillRegistry(root=root / "skills", package_skills=PACKAGE_ROOT / "skills")
        main = VariantSelection.from_registry_row(registry.main_version())
        profile = ModelProfile(
            profile_id="test", protocol="openai-chat-completions",
            base_url="https://model.test/v1", model="test",
        )
        runner = ResearchBatchRunner(
            batches_root=root / "batches", cache_root=root / "cache",
            registry=registry,
                integration_policy=json.loads((PACKAGE_ROOT / "config/integration.json").read_text(encoding="utf-8")),
        )
        result = runner.run(
            evidence=evidence, variants=[main], profile=profile,
            secret="secret", caller=FakeModel(),
        )
        return result["status"]["batch_id"]

    def test_close_same_day_creates_provisional_labels(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / 'research'
            batch_id = self.setup_batch(root)
            refresh_research_labels(research_root=root, batches_root=root / 'batches',
                profile=DataProfile(profile_id='test', universe_file='unused.csv'),
                observed_at=datetime(2026, 9, 4, 16, 5, tzinfo=ZoneInfo('America/New_York')),
                market_provider=LabelMarket())
            path = root / 'batches' / batch_id / 'labels.json'
            self.assertTrue(path.exists())
            self.assertEqual(json.loads(path.read_text())['labels']['AAPL']['status'], 'provisional')

    def test_label_requires_later_independent_matching_observation(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "research"
            batch_id = self.setup_batch(root)
            profile = DataProfile(profile_id="test", universe_file="unused.csv")
            first = refresh_research_labels(
                research_root=root, batches_root=root / "batches", profile=profile,
                observed_at=datetime(2026, 9, 5, 9, tzinfo=ZoneInfo("America/New_York")),
                market_provider=LabelMarket(),
            )
            label_path = root / "batches" / batch_id / "labels.json"
            provisional = json.loads(label_path.read_text(encoding="utf-8"))["labels"]["AAPL"]
            dashboard = ResearchDashboardIndex(
                batches_root=root / "batches", database=root / "index.sqlite3"
            )
            before = dashboard.overview()
            second = refresh_research_labels(
                research_root=root, batches_root=root / "batches", profile=profile,
                observed_at=datetime(2026, 9, 8, 9, tzinfo=ZoneInfo("America/New_York")),
                market_provider=LabelMarket(),
            )
            final = json.loads(label_path.read_text(encoding="utf-8"))["labels"]["AAPL"]
            after = dashboard.overview()
        self.assertEqual(first["refreshed_batches"], [batch_id])
        self.assertEqual(second["refreshed_batches"], [batch_id])
        self.assertEqual(provisional["status"], "provisional")
        self.assertEqual(final["status"], "final")
        self.assertEqual(final["actual_direction"], "bullish")
        self.assertAlmostEqual(final["open_to_close_return"], 0.02)
        self.assertEqual(before["performance"][0]["evaluated"], 0)
        self.assertEqual(after["performance"][0]["evaluated"], 1)
        self.assertEqual(after["performance"][0]["correct"], 1)
        replay = after["daily_results"]
        self.assertEqual(len(replay), 1)
        self.assertEqual(replay[0]["status"], "final")
        self.assertEqual(replay[0]["correct"], 1)
        self.assertEqual(replay[0]["incorrect"], 0)
        self.assertAlmostEqual(replay[0]["daily_pnl"], 2.0)
        # This test replays historical evidence with today's model invocation.
        # The paper return remains visible but cannot become a live forecast.
        self.assertFalse(replay[0]["score_eligible"])
        self.assertAlmostEqual(replay[0]["cumulative_pnl"], 0.0)

    def test_flat_is_neutral_and_directional_prediction_is_wrong(self):
        class FlatMarket(LabelMarket):
            def history(self, symbols, **kwargs):
                rows = super().history(symbols, **kwargs)
                for value in rows.values():
                    value[0]["close"] = value[0]["open"]
                return rows

        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "research"
            self.setup_batch(root)
            profile = DataProfile(profile_id="test", universe_file="unused.csv")
            for day in (5, 8):
                refresh_research_labels(
                    research_root=root, batches_root=root / "batches", profile=profile,
                    observed_at=datetime(2026, 9, day, 9, tzinfo=ZoneInfo("America/New_York")),
                    market_provider=FlatMarket(),
                )
            performance = ResearchDashboardIndex(
                batches_root=root / "batches", database=root / "index.sqlite3"
            ).overview()["performance"][0]
        self.assertEqual(performance["evaluated"], 1)
        self.assertEqual(performance["correct"], 0)


if __name__ == "__main__":
    unittest.main()
