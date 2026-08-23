from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from .hashing import sha256_file, sha256_payload


def _cell(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return html.escape(str(value))


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>{_cell(title)}</title><style>body{{font:15px/1.5 system-ui;margin:36px;max-width:1100px;color:#172033}}h1,h2{{letter-spacing:-.02em}}table{{border-collapse:collapse;width:100%;margin:16px 0}}th,td{{border-bottom:1px solid #d9deea;padding:8px;text-align:left;vertical-align:top}}.muted{{color:#647086}}.bullish{{color:#087443}}.bearish{{color:#b42318}}.neutral,.unavailable{{color:#647086}}code{{font-size:12px;overflow-wrap:anywhere}}</style></head><body>{body}</body></html>"""


def professor_report(
    *, frozen: dict[str, Any], collection_statuses: dict[str, Any] | None,
    orders: dict[str, Any] | None, labels: dict[str, Any] | None,
) -> str:
    prediction_rows = "".join(
        f"<tr><td>{_cell(row['symbol'])}</td><td class={_cell(row['direction'])}>{_cell(row['direction'])}</td><td>{_cell(row['track'])}</td><td>{_cell(row['strongest_countercase'])}</td></tr>"
        for row in frozen.get("predictions", [])
    ) or '<tr><td colspan="4">No candidate passed the frozen gates.</td></tr>'
    status_rows = ""
    if collection_statuses:
        status_rows = "".join(
            f"<tr><td>{_cell(row['symbol'])}</td><td>{_cell(row['domain'])}</td><td>{_cell(row['status'])}</td><td>{_cell(row.get('reason'))}</td></tr>"
            for row in collection_statuses.get("statuses", [])
        )
    order_count = len((orders or {}).get("orders", (orders or {}).get("intents", [])))
    label_count = len((labels or {}).get("labels", []))
    body = (
        f"<h1>Daily Oracle V6 — {_cell(frozen.get('run_id'))}</h1>"
        f"<p class=muted>Mode: {_cell(frozen.get('mode'))} · Target: official unadjusted US regular-session open → close · Frozen hash: <code>{_cell(frozen.get('run_sha256'))}</code></p>"
        "<h2>Frozen predictions</h2><table><thead><tr><th>Symbol</th><th>Direction</th><th>Track</th><th>Strongest countercase</th></tr></thead>"
        f"<tbody>{prediction_rows}</tbody></table>"
        f"<h2>Execution and labels</h2><p>Broker/order records: {order_count}. Scientific label placeholders/results: {label_count}. These ledgers are separate.</p>"
        "<h2>Collector status</h2><table><thead><tr><th>Symbol</th><th>Domain</th><th>Status</th><th>Reason</th></tr></thead>"
        f"<tbody>{status_rows or '<tr><td colspan=4>No deep candidates.</td></tr>'}</tbody></table>"
    )
    return _page("Daily Oracle V6 report", body)


def agent_trace(*, frozen: dict[str, Any]) -> str:
    sections = []
    for symbol, reports in sorted(frozen.get("reports_by_symbol", {}).items()):
        rows = "".join(
            f"<tr><td>{_cell(report['domain'])}</td><td class={_cell(report['verdict'])}>{_cell(report['verdict'])}</td><td>{_cell(report['thesis'])}</td><td>{_cell(report['antithesis'])}</td><td>{_cell(report['unknowns'])}</td><td><code>{_cell(report['lineage_root_ids'])}</code></td></tr>"
            for report in reports
        )
        adversary = frozen.get("adversary_by_symbol", {}).get(symbol, {})
        sections.append(
            f"<h2>{_cell(symbol)}</h2><table><thead><tr><th>Domain</th><th>Verdict</th><th>Thesis</th><th>Antithesis</th><th>Unknowns</th><th>Lineage</th></tr></thead><tbody>{rows}</tbody></table>"
            f"<p><strong>Non-voting adversary:</strong> {_cell(adversary.get('strongest_countercase'))} · veto={_cell(adversary.get('veto'))}</p>"
        )
    return _page("Daily Oracle V6 agent trace", f"<h1>Six-domain trace</h1>{''.join(sections) or '<p>No candidates.</p>'}")


def write_reports(
    *, runtime: Path, frozen: dict[str, Any], collection_statuses: dict[str, Any] | None = None,
    orders: dict[str, Any] | None = None, labels: dict[str, Any] | None = None,
) -> dict[str, Any]:
    outputs = {
        "professor_report.html": professor_report(
            frozen=frozen, collection_statuses=collection_statuses, orders=orders, labels=labels
        ),
        "agent_trace.html": agent_trace(frozen=frozen),
    }
    for name, content in outputs.items():
        path = runtime / name
        path.write_text(content, encoding="utf-8")
    report_manifest = {
        "schema_version": 6,
        "frozen_run_sha256": frozen.get("run_sha256"),
        "reports": {name: sha256_file(runtime / name) for name in sorted(outputs)},
    }
    report_manifest["report_manifest_sha256"] = sha256_payload(report_manifest)
    (runtime / "report_manifest.json").write_text(
        json.dumps(report_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report_manifest
