from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from .hashing import sha256_file, sha256_payload
from .replay import run_replay


def _cell(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return html.escape(str(value))


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>{_cell(title)}</title><style>body{{font:15px/1.6 -apple-system,BlinkMacSystemFont,\"Microsoft YaHei\",sans-serif;margin:36px;max-width:1100px;color:#172033}}h1,h2{{letter-spacing:-.02em}}table{{border-collapse:collapse;width:100%;margin:16px 0}}th,td{{border-bottom:1px solid #d9deea;padding:8px;text-align:left;vertical-align:top}}.muted{{color:#647086}}.bullish{{color:#087443}}.bearish{{color:#b42318}}.neutral,.unavailable{{color:#647086}}code{{font-size:12px;overflow-wrap:anywhere}}</style></head><body>{body}</body></html>"""


_ZH = {
    "bullish": "看涨", "bearish": "看跌", "neutral": "中性",
    "unavailable": "无可用证据", "collected": "已采集",
    "not_entitled": "无数据权限", "no_data": "当天无数据",
    "provider_error": "数据源异常", "not_applicable": "不适用",
    "ordinary": "普通榜", "event": "事件榜", "canary": "正式模拟",
    "shadow": "影子记录", "available": "可用",
    "provisional": "暂定复盘", "final": "最终复盘",
}


def _zh(value: Any) -> str:
    return _ZH.get(str(value), str(value))


def _pct(value: Any) -> str:
    return f"{100 * float(value):.2f}%"


def professor_report(
    *, frozen: dict[str, Any], collection_statuses: dict[str, Any] | None,
    orders: dict[str, Any] | None, labels: dict[str, Any] | None,
    postmortem: dict[str, Any] | None = None,
) -> str:
    prediction_rows = "".join(
        f"<tr><td>{_cell(row['symbol'])}</td><td class={_cell(row['direction'])}>{_cell(_zh(row['direction']))}</td><td>{_cell(_zh(row['track']))}</td><td>{_cell(row['strongest_countercase'])}</td></tr>"
        for row in frozen.get("predictions", [])
    ) or '<tr><td colspan="4">今天没有股票同时满足证据门槛，因此不交易。</td></tr>'
    audit_rows = "".join(
        f"<tr><td>{_cell(symbol)}</td><td>{_cell(row.get('applicable_domain_count'))}</td><td>{_cell(row.get('directional_domain_count'))}</td><td>{_cell(row.get('rejection_reasons'))}</td></tr>"
        for symbol, row in sorted(frozen.get("integration_audit_by_symbol", {}).items())
    )
    status_rows = ""
    if collection_statuses:
        status_rows = "".join(
            f"<tr><td>{_cell(row['symbol'])}</td><td>{_cell(row['domain'])}</td><td>{_cell(_zh(row['status']))}</td><td>{_cell(row.get('reason'))}</td></tr>"
            for row in collection_statuses.get("statuses", [])
        )
    order_count = len((orders or {}).get("orders", (orders or {}).get("intents", [])))
    label_count = len((labels or {}).get("labels", []))
    statuses = (collection_statuses or {}).get("statuses", [])
    true_unavailable = sum(
        row.get("status") in {"not_entitled", "provider_error", "no_data"}
        for row in statuses
    )
    not_applicable = sum(row.get("status") == "not_applicable" for row in statuses)
    retrospective = ""
    if postmortem:
        review_rows = "".join(
            "<tr>"
            f"<td>{_cell(row['symbol'])}</td>"
            f"<td>{_cell(_zh(row['actual_direction']))}</td>"
            f"<td>{_cell(_pct(row['attribution']['stock_open_to_close_return']))}</td>"
            f"<td>{_cell(_pct(row['attribution']['market_component_return']))}</td>"
            f"<td>{_cell(_pct(row['attribution']['industry_component_return']))}</td>"
            f"<td>{_cell(_pct(row['attribution']['stock_specific_component_return']))}</td>"
            f"<td>{'已发布' if row['published'] else '未入榜'}</td>"
            "</tr>"
            for row in postmortem.get("candidate_diagnostics", [])
        )
        uncovered = sum(
            row.get("uncovered_realized_move") is True
            for row in postmortem.get("candidate_diagnostics", [])
        )
        retrospective = (
            f"<h2>收盘后复盘</h2><p>状态：{_cell(_zh(postmortem.get('phase')))}；"
            f"复盘候选：{_cell(postmortem.get('candidate_count'))}只；"
            f"当天有涨跌但未入榜：{uncovered}只。这里的拆分用于诊断，不代表找到了唯一原因。</p>"
            "<table><thead><tr><th>股票</th><th>实际方向</th><th>实际涨跌</th>"
            "<th>大盘带来的部分</th><th>行业额外部分</th><th>股票自身部分</th><th>盘前状态</th>"
            f"</tr></thead><tbody>{review_rows}</tbody></table>"
            "<p class=muted>盘后AI只能提出带出处的研究假设；它不能自动修改第二天的Skill、门槛或下单规则。</p>"
        )
    body = (
        f"<h1>SHAQ Daily Oracle 每日模拟记录</h1>"
        f"<p class=muted>运行编号：{_cell(frozen.get('run_id'))} · 模式：{_cell(_zh(frozen.get('mode')))} · 评价口径：美股常规盘官方开盘价到收盘价 · 冻结校验值：<code>{_cell(frozen.get('run_sha256'))}</code></p>"
        "<h2>盘前已冻结的预测</h2><table><thead><tr><th>股票</th><th>方向</th><th>类别</th><th>最强反方理由</th></tr></thead>"
        f"<tbody>{prediction_rows}</tbody></table>"
        "<h2>候选覆盖与门禁</h2><table><thead><tr><th>股票</th><th>适用领域</th><th>有方向领域</th><th>未入榜原因</th></tr></thead>"
        f"<tbody>{audit_rows or '<tr><td colspan=4>没有候选。</td></tr>'}</tbody></table>"
        f"<h2>模拟成交与结果</h2><p>订单记录：{order_count} 条；开盘到收盘的评价记录：{label_count} 条。交易账和预测成绩分开保存，互不覆盖。</p>"
        f"{retrospective}"
        f"<h2>数据采集情况</h2><p>真实不可用：{true_unavailable} 项；当天不适用：{not_applicable} 项。两者分开记录。</p><table><thead><tr><th>股票</th><th>领域</th><th>状态</th><th>说明</th></tr></thead>"
        f"<tbody>{status_rows or '<tr><td colspan=4>今天没有进入深度分析的候选。</td></tr>'}</tbody></table>"
    )
    return _page("SHAQ Daily Oracle 每日模拟记录", body)


def agent_trace(*, frozen: dict[str, Any]) -> str:
    sections = []
    for symbol, reports in sorted(frozen.get("reports_by_symbol", {}).items()):
        rows = "".join(
            f"<tr><td>{_cell(report['domain'])}</td><td>{_cell(_zh(report.get('availability', 'available')))}</td><td class={_cell(report['verdict'])}>{_cell(_zh(report['verdict']))}</td><td>{_cell(report['thesis'])}</td><td>{_cell(report['antithesis'])}</td><td>{_cell(report['unknowns'])}</td><td><code>{_cell(report['lineage_root_ids'])}</code></td></tr>"
            for report in reports
        )
        adversary = frozen.get("adversary_by_symbol", {}).get(symbol, {})
        sections.append(
            f"<h2>{_cell(symbol)}</h2><table><thead><tr><th>领域</th><th>数据状态</th><th>判断</th><th>支持理由</th><th>反对理由</th><th>未知项</th><th>独立证据来源</th></tr></thead><tbody>{rows}</tbody></table>"
            f"<p><strong>不计票的反方审查：</strong> {_cell(adversary.get('strongest_countercase'))} · 是否否决：{_cell(adversary.get('veto'))}</p>"
        )
    return _page("SHAQ Daily Oracle 六领域审计", f"<h1>六个专业领域如何得出判断</h1>{''.join(sections) or '<p>今天没有候选进入六领域分析。</p>'}")


def write_reports(
    *, runtime: Path, frozen: dict[str, Any], collection_statuses: dict[str, Any] | None = None,
    orders: dict[str, Any] | None = None, labels: dict[str, Any] | None = None,
    postmortem: dict[str, Any] | None = None,
) -> dict[str, Any]:
    outputs = {
        "professor_report.html": professor_report(
            frozen=frozen, collection_statuses=collection_statuses, orders=orders,
            labels=labels, postmortem=postmortem,
        ),
        "agent_trace.html": agent_trace(frozen=frozen),
        "run_replay.html": run_replay(runtime=runtime, frozen=frozen),
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
