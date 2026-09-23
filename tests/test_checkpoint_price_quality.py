"""Incomplete provider prices must be re-read rather than pinned as success."""
import json
import tempfile
import unittest
from pathlib import Path

from shaq_daily_oracle.collection_checkpoint import HistoryCheckpoint
from shaq_daily_oracle.hashing import sha256_payload


class CheckpointPriceQualityTests(unittest.TestCase):
    def test_hashed_checkpoint_with_missing_last_close_is_not_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = HistoryCheckpoint(Path(directory), {"symbol": "AAA"})
            rows = [{"timestamp": "2026-09-21", "close": 100},
                    {"timestamp": "2026-09-22", "close": None}]
            checkpoint.path.write_text(json.dumps({"request": checkpoint.request,
                "rows": rows, "rows_sha256": sha256_payload(rows)}))
            self.assertIsNone(checkpoint.read())

    def test_incomplete_price_response_is_not_saved_as_success(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = HistoryCheckpoint(Path(directory), {"symbol": "AAA"})
            checkpoint.save([{"timestamp": "2026-09-22", "close": None}])
            self.assertFalse(checkpoint.path.exists())
            checkpoint.save([{"timestamp": "2026-09-22", "close": 101}])
            self.assertEqual(checkpoint.read()[0]["close"], 101)
