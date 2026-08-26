from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .batch_review import build_batch_review
from .postmortem_runner import _write_immutable


class BatchReviewRunner:
    def __init__(self, *, package_root: Path, runtime_root: Path) -> None:
        self.package_root = package_root.resolve()
        self.runtime_root = runtime_root.resolve()

    def run(self, *, generated_at_et: datetime) -> Path:
        config = json.loads(
            (self.package_root / "config/postmortem.json").read_text(encoding="utf-8")
        )
        readiness = json.loads(
            (self.package_root / "config/readiness.json").read_text(encoding="utf-8")
        )
        documents: list[dict[str, Any]] = []
        for path in sorted(self.runtime_root.glob("SHAQ-CANARY-*/postmortem/postmortem_final.json")):
            documents.append(json.loads(path.read_text(encoding="utf-8")))
        if not documents:
            raise ValueError("no final postmortems are available for batch review")
        result = build_batch_review(
            postmortems=documents,
            generated_at_et=generated_at_et.isoformat(),
            review_interval_sessions=int(config["batch_review_interval_trading_days"]),
            drift_delta=float(config["drift_alert_delta"]),
            minimum_promotion_days=int(readiness["probability"]["minimum_trading_days"]),
            minimum_promotion_forecasts=int(
                readiness["probability"]["minimum_evaluated_forecasts"]
            ),
        )
        output = self.runtime_root / "postmortem_reviews" / (
            f"batch-review-{documents[0]['trade_date']}-to-{documents[-1]['trade_date']}.json"
        )
        if output.exists():
            existing = json.loads(output.read_text(encoding="utf-8"))
            if existing.get("final_postmortem_sha256s") != result["final_postmortem_sha256s"]:
                raise ValueError("an immutable batch review already covers a different data set")
            return output
        _write_immutable(output, result)
        return output
