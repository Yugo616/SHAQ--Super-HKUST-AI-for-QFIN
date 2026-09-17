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
            lines = self.path.read_bytes().splitlines()
        except OSError:
            return []
        rows = []
        for line in lines:
            try:
                row = json.loads(line.decode("utf-8"))
            except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
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


def summarize_job(job: dict[str, Any]) -> dict[str, Any]:
    """Read-only projection of declared tasks and saved events, never a forecast."""
    events = job.get('research_progress', [])
    variants = {}
    calls = {}
    for key, status in job.get('variant_progress', {}).items():
        rows = [row for row in events if row.get('variant_key') == key]
        plans = [row for row in rows if row.get('stage') == 'tasks_planned']
        tasks = {task['task_id'] for task in plans[-1].get('tasks', [])} if plans else None
        completed = set()
        for row in rows:
            stage = row.get('stage')
            task = (f"report:{row.get('symbol')}:{row.get('domain')}" if stage == 'report_validated'
                    else 'decision' if stage == 'decision_complete'
                    else stage if stage in {'adversary', 'synthesis'} and row.get('status') == 'complete'
                    else None)
            if tasks is not None and task in tasks:
                completed.add(task)
            if tasks is not None and stage == 'variant_reused' and row.get('status') == 'complete':
                completed.update(tasks)
            if row.get('call_id') and stage in {'call_requested', 'model_started', 'model_returned', 'cache_hit', 'failure', 'validation_failure'}:
                identity = (key, row['call_id'])
                attempt = int(row.get('attempt', 1))
                if attempt >= calls.get(identity, (0, ''))[0]:
                    calls[identity] = (attempt, stage)
        variants[key] = dict(status=status, total_tasks=len(tasks) if tasks is not None else None,
                             completed_tasks=len(completed))
    known = bool(variants) and all(row['total_tasks'] is not None for row in variants.values())
    state = job.get('status', 'queued')
    stage = 'preparation'
    for row in events:
        event_stage = row.get('stage')
        if event_stage in {'preparation', 'screening', 'adversary', 'synthesis', 'decision_complete'}:
            stage = 'decision' if event_stage in {'synthesis', 'decision_complete'} else event_stage
        elif event_stage in {'tasks_planned', 'report_validated', 'model_started'}:
            stage = 'domain_analysis' if row.get('domain') or event_stage != 'model_started' else stage
    if state not in {'queued', 'running'}:
        stage = 'complete' if state == 'complete' else 'incomplete'
    observed = max([str(row.get('occurred_at_et') or '') for row in events] +
                   [str(job.get('completed_at_et') or job.get('started_at_et') or '')])
    try:
        started = datetime.fromisoformat(job.get('started_at_et', ''))
        trade_date = started.astimezone(ET).date().isoformat() if started.tzinfo else None
    except (TypeError, ValueError):
        trade_date = None
    return dict(scope='research_run', job_id=job.get('job_id'), batch_id=job.get('batch_id'),
                trade_date=trade_date, status=state, stage=stage, variants=variants,
                completed_tasks=sum(row['completed_tasks'] for row in variants.values()),
                total_tasks=sum(row['total_tasks'] for row in variants.values()) if known else None,
                completed_calls=sum(stage == 'model_returned' for _, stage in calls.values()),
                reused_calls=sum(stage == 'cache_hit' for _, stage in calls.values()),
                observed_calls=len(calls), total_calls=None,
                started_at=job.get('started_at_et'), completed_at=job.get('completed_at_et'),
                last_event_at=observed or None)
