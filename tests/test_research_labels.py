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
from shaq_daily_oracle.research_labels import refresh_research_labels
from shaq_daily_oracle.skill_versions import LocalSkillRegistry
from tests.test_research_batch import FakeModel


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
                observed_at=datetime(2026, 9, 6, 9, tzinfo=ZoneInfo("America/New_York")),
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
            for day in (5, 6):
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
