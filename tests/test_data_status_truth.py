"""The data panel must distinguish today's qualified data from old or unusable evidence."""

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from shaq_daily_oracle.hashing import sha256_payload
from shaq_daily_oracle.lab_service import LabService


ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 23, 9, 0, tzinfo=ET)


class DataStatusTruthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        package = root / "package"
        (package / "config").mkdir(parents=True)
        (package / "config/research-universe.csv").write_text(
            "instrument,source_role,observed_active_at_utc\n"
            "AAA,versioned,2026-01-01T00:00:00Z\n", encoding="utf-8")
        self.paths = SimpleNamespace(package_root=package, research_root=root / "research")
        self.service = object.__new__(LabService)
        self.service.paths = self.paths
        self.service.settings = Mock()
        self.service.settings.load.return_value = {"data_profile": {
            "profile_id": "test", "universe_file": "config/research-universe.csv",
        }}

    def tearDown(self):
        self.temp.cleanup()

    def manifest(self, as_of, *, collection, observations=None, metadata_status="collected"):
        provider = {"collection_statuses": collection,
                    "premarket_observations": observations or {},
                    "metadata_status": metadata_status,
                    "public_source_statuses": []}
        value = {"as_of_et": as_of, "cutoff_status": "on_time",
                 "provider_manifest": provider}
        digest = sha256_payload(value)
        value["evidence_hash"] = digest
        evidence = self.paths.research_root / "evidence" / digest
        evidence.mkdir(parents=True)
        (evidence / "evidence_manifest.json").write_text(json.dumps(value), encoding="utf-8")
        locators = self.paths.research_root / "evidence_sessions"
        locators.mkdir(parents=True)
        (locators / f"{as_of[:10]}-test.json").write_text(
            json.dumps({"evidence_hash": digest}), encoding="utf-8")

    def status(self):
        with patch("shaq_daily_oracle.lab_service.datetime", wraps=datetime) as clock:
            clock.now.return_value = NOW
            return self.service.data_status()

    @staticmethod
    def item(result, name):
        return next(row for row in result["items"] if row["name"] == name)

    def test_yesterday_evidence_is_historical_not_today_fresh(self):
        # Catches the previous unconditional fresh badge for any old manifest.
        self.manifest("2026-09-22T08:45:00-04:00", collection=[
            {"symbol": "*", "domain": "market", "status": "collected"},
            {"symbol": "AAA", "domain": "event", "status": "not_applicable"},
        ])
        result = self.status()
        self.assertEqual(self.item(result, "股票、市场与行业行情")["status"], "historical")
        self.assertEqual(self.item(result, "公司一手公告")["status"], "historical")
        self.assertIn("历史", self.item(result, "股票、市场与行业行情")["note"])

    def test_today_zero_volume_and_unusable_option_quotes_are_not_success(self):
        # Catches treating provider zero volume and un-timestamped options as real signals.
        self.manifest("2026-09-23T08:45:00-04:00", collection=[
            {"symbol": "*", "domain": "market", "status": "collected"},
            {"symbol": "AAA", "domain": "event", "status": "not_applicable"},
            {"symbol": "AAA", "domain": "derivatives", "status": "no_data",
             "reason": "missing_exchange_quote_timestamp",
             "source_contract_count": 100, "retained_contract_count": 10,
             "valid_two_sided_price_count": 2, "quote_timestamp_status": "unavailable"},
        ], observations={"AAA": {"status": "collected", "eligible_price_bar_count": 5,
                                  "volume_status": "volume_unavailable",
                                  "zero_volume_bar_count": 5}})
        result = self.status()
        market = self.item(result, "股票、市场与行业行情")
        event = self.item(result, "公司一手公告")
        options = self.item(result, "期权报价与持仓结构")
        self.assertIn("真实成交量未知", market["note"])
        self.assertNotIn("盘前成交量0", market["coverage"] + market["note"])
        self.assertEqual(event["status"], "not_applicable")
        self.assertIn("无事件", event["note"])
        self.assertEqual(options["status"], "limited")
        self.assertIn("100", options["coverage"])
        self.assertIn("10", options["coverage"])
        self.assertIn("2", options["coverage"])
        self.assertIn("报价时间", options["note"])

    def test_today_provider_error_is_not_no_event_or_fresh_metadata(self):
        # Catches conflating transport failure with a legitimate empty event day.
        self.manifest("2026-09-23T08:45:00-04:00", collection=[
            {"symbol": "*", "domain": "market", "status": "collected"},
            {"symbol": "AAA", "domain": "event", "status": "provider_error"},
            {"symbol": "AAA", "domain": "derivatives", "status": "provider_error",
             "reason": "provider_error"},
        ], metadata_status="provider_error:DataProviderError")
        result = self.status()
        self.assertEqual(self.item(result, "公司一手公告")["status"], "provider_error")
        self.assertNotIn("无事件", self.item(result, "公司一手公告")["note"])
        self.assertEqual(self.item(result, "期权报价与持仓结构")["status"], "provider_error")
        self.assertEqual(self.item(result, "证券身份与行业资料")["status"], "provider_error")

    def test_no_option_chain_is_distinct_from_missing_quote_time(self):
        # Catches interpreting an empty chain as a captured but untimed quote.
        self.manifest("2026-09-23T08:45:00-04:00", collection=[
            {"symbol": "*", "domain": "market", "status": "collected"},
            {"symbol": "AAA", "domain": "derivatives", "status": "no_data",
             "reason": "no_option_chain", "source_contract_count": 0,
             "retained_contract_count": 0, "valid_two_sided_price_count": 0},
        ])
        option = self.item(self.status(), "期权报价与持仓结构")
        self.assertEqual(option["status"], "unavailable")
        self.assertIn("未取得期权链", option["note"])
        self.assertNotIn("缺少交易所报价时间", option["note"])

    def test_legacy_option_counts_are_unknown_not_invented_zero(self):
        # Catches treating fields absent from an older manifest as measured zeros.
        self.manifest("2026-09-23T08:45:00-04:00", collection=[
            {"symbol": "*", "domain": "market", "status": "collected"},
            {"symbol": "AAA", "domain": "derivatives", "status": "no_data"},
        ])
        option = self.item(self.status(), "期权报价与持仓结构")
        self.assertIn("源合约未记录", option["coverage"])
        self.assertIn("保留未记录", option["coverage"])
        self.assertIn("有效双边价未记录", option["coverage"])

    def test_missing_exhibit_is_not_reported_as_complete_earnings(self):
        self.manifest("2026-09-23T08:45:00-04:00", collection=[
            {"symbol": "AAA", "domain": "event", "status": "collected", "partial_failure": True},
        ])
        row = self.item(self.status(), "公司一手公告")
        self.assertEqual(row["status"], "partial_failure")
        self.assertIn("附件", row["note"])

    def test_occ_interest_is_not_described_as_usable_quotes(self):
        self.manifest("2026-09-23T08:45:00-04:00", collection=[
            {"symbol": "AAA", "domain": "derivatives", "status": "collected",
             "reason": "open_interest_context_only", "open_interest_context_available": True,
             "open_interest_context_contracts": 120},
        ])
        row = self.item(self.status(), "期权报价与持仓结构")
        self.assertEqual(row["status"], "limited")
        self.assertIn("OCC", row["coverage"])
        self.assertIn("不包含报价", row["note"])


if __name__ == "__main__":
    unittest.main()
