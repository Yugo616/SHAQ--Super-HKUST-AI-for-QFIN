from __future__ import annotations

import html
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .hashing import sha256_file, sha256_payload


class DashboardError(ValueError):
    """The derived dashboard index cannot represent an immutable run safely."""


def _read(path: Path, fallback: Any = None) -> Any:
    if not path.is_file():
        return fallback
    return json.loads(path.read_text(encoding="utf-8"))


def _valid_frozen(path: Path) -> tuple[dict[str, Any], bool]:
    value = _read(path, {})
    if not isinstance(value, dict) or not value:
        return {}, False
    declared = value.get("run_sha256")
    unsigned = dict(value)
    unsigned.pop("run_sha256", None)
    return value, bool(declared and declared == sha256_payload(unsigned))


class DashboardIndex:
    """A disposable SQLite view; immutable runtime files remain the source of truth."""

    def __init__(self, *, runtime_root: Path, database: Path) -> None:
        self.runtime_root = runtime_root.resolve()
        self.database = database.resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(runs)").fetchall()
        }
        if columns and "excluded" not in columns:
            connection.executescript(
                "DROP TABLE IF EXISTS predictions; DROP TABLE IF EXISTS round_trips; "
                "DROP TABLE IF EXISTS runs;"
            )
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
              run_id TEXT PRIMARY KEY,
              trade_date TEXT NOT NULL,
              mode TEXT NOT NULL,
              status TEXT NOT NULL,
              system_identity TEXT,
              run_sha256 TEXT,
              source_sha256 TEXT NOT NULL,
              candidate_count INTEGER NOT NULL,
              prediction_count INTEGER NOT NULL,
              order_count INTEGER NOT NULL,
              net_pnl REAL,
              fees REAL,
              audit_valid INTEGER NOT NULL,
              excluded INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS predictions (
              run_id TEXT NOT NULL,
              symbol TEXT NOT NULL,
              direction TEXT NOT NULL,
              track TEXT,
              PRIMARY KEY (run_id, symbol)
            );
            CREATE TABLE IF NOT EXISTS round_trips (
              run_id TEXT NOT NULL,
              symbol TEXT NOT NULL,
              trade_date TEXT,
              net_pnl REAL,
              fees REAL,
              outcome_status TEXT,
              PRIMARY KEY (run_id, symbol)
            );
            """
        )
        return connection

    def rebuild(self) -> None:
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute("DELETE FROM predictions")
            connection.execute("DELETE FROM round_trips")
            connection.execute("DELETE FROM runs")
            for directory in sorted(self.runtime_root.glob("SHAQ-CANARY-*-*")):
                frozen_path = directory / "frozen_run.json"
                frozen, audit_valid = _valid_frozen(frozen_path)
                identity = _read(directory / "workflow_identity.json", {})
                if not frozen:
                    continue
                run_id = str(frozen.get("run_id", directory.name))
                trade_date = str(
                    identity.get("trade_date")
                    or frozen.get("created_at", frozen.get("cutoff_et", ""))[:10]
                )
                journal = _read(directory / "broker_journal.json", {"orders": {}})
                ledger = _read(directory / "execution_ledger.json", {"round_trips": []})
                round_trips = ledger.get("round_trips", [])
                corrections = (
                    _read(path, {}) for path in directory.glob("correction_*.json")
                )
                excluded = any(
                    bool(document.get("excluded_from_professor_summary"))
                    or "excluded" in str(document.get("professor_summary_policy", "")).lower()
                    for document in corrections
                )
                net_values = [row.get("net_pnl") for row in round_trips if row.get("net_pnl") is not None]
                fee_values = [row.get("fees") for row in round_trips if row.get("fees") is not None]
                status = "complete" if (directory / "audit_complete.json").is_file() else "running"
                if (directory / "workflow_failure.json").is_file():
                    status = "failed"
                connection.execute(
                    "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id, trade_date, str(frozen.get("mode", "unknown")), status,
                        identity.get("system_identity"), frozen.get("run_sha256"),
                        sha256_file(frozen_path),
                        len(frozen.get("reports_by_symbol", {})),
                        len(frozen.get("predictions", [])),
                        len(journal.get("orders", {})),
                        sum(float(value) for value in net_values) if net_values else None,
                        sum(float(value) for value in fee_values) if fee_values else None,
                        1 if audit_valid else 0,
                        1 if excluded else 0,
                    ),
                )
                if not audit_valid:
                    continue
                for row in frozen.get("predictions", []):
                    connection.execute(
                        "INSERT INTO predictions VALUES (?, ?, ?, ?)",
                        (run_id, row["symbol"], row["direction"], row.get("track")),
                    )
                for row in round_trips:
                    connection.execute(
                        "INSERT OR REPLACE INTO round_trips VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            run_id, row.get("symbol"), row.get("trade_date"), row.get("net_pnl"),
                            row.get("fees"), row.get("outcome_status"),
                        ),
                    )
            connection.commit()

    def _runtime(self, run_id: str) -> Path:
        if Path(run_id).name != run_id:
            raise DashboardError("invalid run identity")
        runtime = (self.runtime_root / run_id).resolve()
        try:
            runtime.relative_to(self.runtime_root)
        except ValueError as exc:
            raise DashboardError("run escapes the configured runtime root") from exc
        return runtime

    def overview(self) -> dict[str, Any]:
        self.rebuild()
        with closing(self._connect()) as connection:
            rows = [dict(row) for row in connection.execute(
                "SELECT * FROM runs ORDER BY trade_date DESC, run_id DESC"
            )]
            totals = dict(connection.execute(
                "SELECT COUNT(*) AS runs, COALESCE(SUM(prediction_count), 0) AS predictions, "
                "COALESCE(SUM(order_count), 0) AS orders, COALESCE(SUM(net_pnl), 0) AS net_pnl, "
                "COALESCE(SUM(fees), 0) AS fees FROM runs WHERE audit_valid = 1 AND excluded = 0"
            ).fetchone())
        latest = self.run_detail(rows[0]["run_id"]) if rows else None
        health = _read(self.runtime_root / "service_status.json", {})
        incident = _read(self.runtime_root / "campaign_failure_latest.json", {})
        if incident:
            health = {**health, "latest_incident": {
                "recorded_at_et": incident.get("recorded_at_et"),
                "stage": incident.get("stage"),
                "error_type": incident.get("error_type"),
                "message": incident.get("message"),
                "impact": incident.get("impact"),
            }}
        shadow_incident = _read(self.runtime_root / "shadow_failure_latest.json", {})
        if shadow_incident:
            health = {**health, "latest_shadow_incident": shadow_incident}
        return {
            "generated_at_et": datetime.now(ZoneInfo("America/New_York")).isoformat(),
            "runs": rows,
            "totals": totals,
            "performance": self._performance(rows),
            "latest": latest,
            "health": health,
        }

    def _performance(self, run_rows: list[dict[str, Any]]) -> dict[str, Any]:
        equity = 0.0
        peak = 0.0
        maximum_drawdown = 0.0
        long_predictions = 0
        short_predictions = 0
        sectors: dict[str, int] = {}
        valid_rows = sorted(
            (
                row for row in run_rows
                if row.get("audit_valid") and not row.get("excluded")
            ),
            key=lambda row: (row["trade_date"], row["run_id"]),
        )
        for row in valid_rows:
            runtime = self._runtime(row["run_id"])
            frozen, valid = _valid_frozen(runtime / "frozen_run.json")
            if not valid:
                continue
            predictions = frozen.get("predictions", [])
            intake = _read(runtime / "candidate_seed_intake.json", {})
            candidates = intake.get("candidates", intake if isinstance(intake, list) else [])
            sector_by_symbol = {
                str(candidate.get("symbol")): str(candidate.get("gics_sector") or "未分类")
                for candidate in candidates
                if isinstance(candidate, dict) and candidate.get("symbol")
            }
            for prediction in predictions:
                if prediction.get("direction") == "bullish":
                    long_predictions += 1
                elif prediction.get("direction") == "bearish":
                    short_predictions += 1
                sector = sector_by_symbol.get(str(prediction.get("symbol")), "未分类")
                sectors[sector] = sectors.get(sector, 0) + 1
            ledger = _read(runtime / "execution_ledger.json", {"round_trips": []})
            daily_pnl = sum(
                float(trade["net_pnl"])
                for trade in ledger.get("round_trips", [])
                if trade.get("net_pnl") is not None
            )
            equity += daily_pnl
            peak = max(peak, equity)
            maximum_drawdown = min(maximum_drawdown, equity - peak)
        return {
            "maximum_drawdown": maximum_drawdown,
            "long_predictions": long_predictions,
            "short_predictions": short_predictions,
            "sector_predictions": dict(
                sorted(sectors.items(), key=lambda item: (-item[1], item[0]))
            ),
        }

    def run_detail(self, run_id: str) -> dict[str, Any]:
        runtime = self._runtime(run_id)
        frozen, audit_valid = _valid_frozen(runtime / "frozen_run.json")
        if not frozen:
            raise DashboardError("the selected run has no frozen result")
        identity = _read(runtime / "workflow_identity.json", {})
        journal = _read(runtime / "broker_journal.json", {"orders": {}})
        ledger = _read(runtime / "execution_ledger.json", {"round_trips": []})
        positions_document = _read(runtime / "portfolio_post_entry.json") or _read(
            runtime / "portfolio_snapshot.json", {"positions": []}
        )
        reports = frozen.get("reports_by_symbol", {}) if audit_valid else {}
        return {
            "run_id": run_id,
            "audit_valid": audit_valid,
            "trade_date": identity.get("trade_date", str(frozen.get("cutoff_et", ""))[:10]),
            "mode": frozen.get("mode"),
            "status": "complete" if (runtime / "audit_complete.json").is_file() else "running",
            "identity": identity.get("system_identity"),
            "cutoff_et": frozen.get("cutoff_et"),
            "predictions": frozen.get("predictions", []) if audit_valid else [],
            "reports_by_symbol": reports,
            "adversary_by_symbol": frozen.get("adversary_by_symbol", {}) if audit_valid else {},
            "integration_audit_by_symbol": frozen.get("integration_audit_by_symbol", {}) if audit_valid else {},
            "orders": list(journal.get("orders", {}).values()),
            "round_trips": ledger.get("round_trips", []),
            "positions": positions_document.get("positions", []),
            "workflow": _read(runtime / "workflow_state.json", {}),
            "has_replay": (runtime / "run_replay.html").is_file(),
            "has_professor_report": (runtime / "professor_report.html").is_file(),
        }

    def export_professor_report(self, destination: Path) -> Path:
        overview = self.overview()
        rows = [row for row in overview["runs"] if not row.get("excluded")]
        body_rows = "".join(
            "<tr>"
            f"<td>{html.escape(row['trade_date'])}</td>"
            f"<td>{html.escape(row['mode'])}</td>"
            f"<td>{row['prediction_count']}</td>"
            f"<td>{row['order_count']}</td>"
            f"<td>{'通过' if row['audit_valid'] else '审计失败'}</td>"
            "</tr>"
            for row in rows
        ) or "<tr><td colspan='5'>尚无运行记录</td></tr>"
        totals = overview["totals"]
        document = f"""<!doctype html><html lang='zh-CN'><meta charset='utf-8'>
<title>SHAQ Daily Oracle 教授报告</title><style>
body{{font-family:Arial,'Microsoft YaHei',sans-serif;max-width:980px;margin:48px auto;color:#102a43}}
h1{{color:#1769aa}}.summary{{display:flex;gap:32px;padding:22px;background:#f2f7fb}}
table{{border-collapse:collapse;width:100%;margin-top:28px}}th,td{{padding:12px;border-bottom:1px solid #d9e2ec;text-align:left}}
</style><h1>SHAQ Daily Oracle 模拟运行摘要</h1>
<p>本页由不可变运行记录生成，已隐藏账户、密钥、本地路径和原始付费数据。</p>
<div class='summary'><b>{totals['runs']} 次运行</b><b>{totals['predictions']} 个预测</b>
<b>{totals['orders']} 条订单</b><b>模拟净盈亏 {float(totals['net_pnl']):.2f}</b></div>
<table><thead><tr><th>日期</th><th>模式</th><th>预测</th><th>订单</th><th>审计</th></tr></thead>
<tbody>{body_rows}</tbody></table></html>"""
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(document, encoding="utf-8")
        return destination
