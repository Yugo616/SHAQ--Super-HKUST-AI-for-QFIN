from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from filelock import FileLock


ET = ZoneInfo("America/New_York")
_FORBIDDEN = {"raw_output", "raw_result", "prompt", "schema"}


class ResearchProgressLog:
    """Append-only, display-only research events; never part of batch identity."""

    def __init__(self, path: Path, clock: Callable[[], str] | None = None) -> None:
        self.path = path
        self.clock = clock or (lambda: datetime.now(ET).isoformat())
        self._thread_lock = threading.Lock()

    def append(self, *, stage: str, batch_id: str = "", **fields: Any) -> None:
        if _FORBIDDEN & fields.keys():
            raise ValueError("raw model inputs or outputs cannot be progress events")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(self.path) + ".lock")
        with self._thread_lock, lock:
            previous = self.read()
            sequence = len(previous) + 1
            if fields.get("call_id") and "attempt" not in fields:
                fields["attempt"] = 1 + sum(
                    row.get("stage") == "domain_call_start"
                    and row.get("call_id") == fields["call_id"] for row in previous
                )
            event = {
                "schema_version": 1,
                "sequence": sequence,
                "occurred_at_et": self.clock(),
                "stage": str(stage),
                "batch_id": str(batch_id),
                **fields,
            }
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n")
                handle.flush()

    def read(self) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        rows = []
        for line in lines:
            try:
                row = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(row, dict) and isinstance(row.get("sequence"), int):
                rows.append(row)
        return sorted(rows, key=lambda row: row["sequence"])


def safe_observe(observer: Callable[..., None] | None, **event: Any) -> None:
    if observer is None:
        return
    try:
        observer(**event)
    except Exception:
        pass
