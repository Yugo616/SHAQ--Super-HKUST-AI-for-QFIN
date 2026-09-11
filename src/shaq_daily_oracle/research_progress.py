from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from filelock import FileLock, Timeout


ET = ZoneInfo("America/New_York")
_FORBIDDEN = {"raw_output", "raw_result", "prompt", "schema"}


class ResearchProgressLog:
    """Append-only, display-only research events; never part of batch identity."""

    def __init__(self, path: Path, clock: Callable[[], str] | None = None) -> None:
        self.path = path
        self.clock = clock or (lambda: datetime.now(ET).isoformat())
        self._thread_lock = threading.Lock()

    def append(self, *, stage: str, batch_id: str = "", **fields: Any) -> dict[str, Any] | None:
        if _FORBIDDEN & fields.keys():
            raise ValueError("raw model inputs or outputs cannot be progress events")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        lock = FileLock(str(self.path) + ".lock")
        if not self._thread_lock.acquire(blocking=False):
            return None
        try:
            lock.acquire(timeout=0)
        except Timeout:
            self._thread_lock.release()
            return None
        try:
            previous = self.read()
            sequence = len(previous) + 1
            if fields.get("call_id") and "attempt" not in fields:
                fields["attempt"] = 1 + sum(
                    row.get("stage") == "call_requested"
                    and row.get("call_id") == fields["call_id"]
                    and row.get("variant_key") == fields.get("variant_key") for row in previous
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
            return event
        finally:
            lock.release()
            self._thread_lock.release()

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


def safe_observe(observer: Callable[..., None] | None, **event: Any) -> Any:
    if observer is None:
        return None
    try:
        return observer(**event)
    except Exception:
        return None
