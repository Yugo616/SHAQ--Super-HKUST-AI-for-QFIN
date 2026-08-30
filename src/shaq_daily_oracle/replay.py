from __future__ import annotations

import html
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .hashing import sha256_file, sha256_payload


class ReplayAuditError(ValueError):
    """A human-readable replay cannot be built from unverified artifacts."""


DOMAIN_ORDER = ("market", "relationships", "event", "capital", "derivatives", "price_volume")
DOMAIN_ZH = {
    "market": "市场共同冲击",
    "relationships": "行业与公司关系",
    "event": "公司事件",
    "capital": "资金与订单流",
    "derivatives": "期权与衍生品",
    "price_volume": "价格与成交量",
}
VERDICT_ZH = {
    "bullish": "偏多",
    "bearish": "偏空",
    "neutral": "中性",
    "not_applicable": "当天不适用",
    "unavailable": "数据不可用",
}
REJECTION_ZH = {
    "fewer_than_two_directional_domains": "少于两个领域给出同向判断",
    "independent_direction_conflict": "独立证据方向冲突",
    "lineage_root_role_gate_not_met": "缺少市场/行业背景或个股自身证据",
    "adversary_integrity_veto": "反方发现完整性问题并否决",
}


def _escape(value: Any) -> str:
    if value is None:
        return "—"
    return html.escape(str(value), quote=True)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _inside_runtime(runtime: Path, value: str | Path) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = runtime / candidate
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(runtime.resolve())
    except ValueError as exc:
        raise ReplayAuditError("审计文件指向运行目录之外") from exc
    return resolved


def _relative_href(runtime: Path, value: str | Path | None) -> str | None:
    if not value:
        return None
    try:
        path = _inside_runtime(runtime, value)
    except (OSError, ReplayAuditError):
        return None
    return quote(path.relative_to(runtime.resolve()).as_posix(), safe="/")


def verify_replay_inputs(runtime: Path, frozen: dict[str, Any]) -> dict[str, Any]:
    """Re-hash every decision-bearing artifact before rendering the replay."""

    issues: list[str] = []
    declared_run_hash = str(frozen.get("run_sha256", ""))
    frozen_payload = {key: value for key, value in frozen.items() if key != "run_sha256"}
    if len(declared_run_hash) != 64 or sha256_payload(frozen_payload) != declared_run_hash:
        issues.append("冻结预测校验值不一致")

    intake_path = runtime / "candidate_intake.json"
    declared_intake_hash = frozen.get("candidate_intake_sha256")
    if declared_intake_hash:
        if not intake_path.is_file() or sha256_file(intake_path) != declared_intake_hash:
            issues.append("候选清单校验值不一致")

    calls_dir = runtime / "ai_calls"
    call_hashes: dict[str, str] = {}
    if calls_dir.is_dir():
        for call_path in sorted(calls_dir.glob("*.json")):
            call = _read_json(call_path)
            if call is None:
                issues.append(f"AI调用文件无法读取：{call_path.name}")
                continue
            audit = call.get("audit", {})
            raw_output = call.get("raw_output")
            if not isinstance(audit, dict) or sha256_payload(raw_output) != audit.get("output_sha256"):
                issues.append(f"AI输出校验失败：{call_path.name}")
            for field, fallback in (("prompt_file", f"{call_path.stem}.prompt"), ("packet_file", f"{call_path.stem}.packet")):
                artifact_name = call.get(field, fallback)
                artifact_hash = call.get(f"{field}_sha256")
                artifact_path = calls_dir / str(artifact_name)
                if artifact_hash and (not artifact_path.is_file() or sha256_file(artifact_path) != artifact_hash):
                    issues.append(f"AI输入校验失败：{artifact_path.name}")
            call_hashes[call_path.stem] = sha256_file(call_path)

    for record in frozen.get("lineage", {}).get("records", []):
        if not isinstance(record, dict):
            issues.append("证据谱系记录格式错误")
            continue
        for path_key, hash_key in (("raw_file_path", "raw_sha256"), ("analysis_file_path", "analysis_sha256")):
            value, expected = record.get(path_key), record.get(hash_key)
            if not value:
                continue
            try:
                path = _inside_runtime(runtime, str(value))
            except (OSError, ReplayAuditError):
                issues.append(f"证据文件不存在或越界：{record.get('evidence_id')}")
                continue
            if expected and sha256_file(path) != expected:
                issues.append(f"证据校验失败：{record.get('evidence_id')}")

    audit_complete = _read_json(runtime / "audit_complete.json")
    audit_status = "awaiting_audit"
    if audit_complete:
        audit_status = str(audit_complete.get("status", "unknown"))
        if audit_complete.get("run_sha256") != declared_run_hash:
            issues.append("完成审计没有绑定当前冻结预测")
        for name, expected in audit_complete.get("formal_ai_call_sha256", {}).items():
            if call_hashes.get(name) != expected:
                issues.append(f"完成审计中的AI调用校验失败：{name}")

    if issues:
        raise ReplayAuditError("；".join(sorted(set(issues))))
    return {
        "status": audit_status,
        "run_sha256": declared_run_hash,
        "verified_ai_call_count": len(call_hashes),
        "verified_evidence_count": len(frozen.get("lineage", {}).get("records", [])),
    }


def _pct(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    try:
        return f"{100 * float(value):+.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def _number(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{int(round(float(value))):,}"
    except (TypeError, ValueError):
        return "—"


def _time(value: Any) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    return parsed.strftime("%H:%M:%S ET")


def _load_snapshot(runtime: Path, folder: str, symbol: str) -> dict[str, Any]:
    return _read_json(runtime / "evidence" / folder / f"{symbol}.json") or {}


def _candidate_metrics(runtime: Path, row: dict[str, Any]) -> dict[str, Any]:
    symbol = str(row["symbol"])
    stock = _load_snapshot(runtime, "stocks_by_symbol", symbol)
    benchmark_symbol = str(row.get("sector_benchmark", ""))
    benchmark = _load_snapshot(runtime, "benchmarks_by_symbol", benchmark_symbol) if benchmark_symbol else {}
    stock_return = row.get("stock_premarket_return")
    if stock_return is None:
        stock_return = stock.get("premarket_semantics", {}).get("premarket_return")
    sector_return = row.get("sector_premarket_return")
    if sector_return is None:
        sector_return = benchmark.get("premarket_semantics", {}).get("premarket_return")
    residual = row.get("residual_premarket_return")
    if residual is None and stock_return is not None and sector_return is not None:
        residual = float(stock_return) - float(sector_return)
    volume = row.get("premarket_volume")
    if volume is None:
        volume = stock.get("raw_snapshot", {}).get("pre_volume")
    raw = stock.get("raw_snapshot", {})
    return {
        "stock_return": stock_return,
        "sector_return": sector_return,
        "residual": residual,
        "volume": volume,
        "pre_low": raw.get("pre_low_price"),
        "pre_high": raw.get("pre_high_price"),
        "sector_benchmark": benchmark_symbol or "—",
    }


def _analysis_metrics(runtime: Path, report: dict[str, Any], evidence_records: dict[str, dict[str, Any]]) -> dict[str, Any]:
    for evidence_id in report.get("evidence_ids", []):
        record = evidence_records.get(str(evidence_id), {})
        analysis_path = record.get("analysis_file_path")
        if not analysis_path:
            continue
        try:
            value = _read_json(_inside_runtime(runtime, str(analysis_path)))
        except (OSError, ReplayAuditError):
            value = None
        if value:
            return value.get("metrics", {}) if isinstance(value.get("metrics"), dict) else {}
    return {}


def _plain_chinese(
    *, runtime: Path, report: dict[str, Any], candidate: dict[str, Any],
    metrics: dict[str, Any], evidence_records: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    domain = str(report["domain"])
    verdict = str(report["verdict"])
    observed = {
        "market": "大盘、成长股、小盘股、利率、美元、信用、波动率与行业ETF在截止前的变化。",
        "relationships": "行业归属、行业ETF暴露，以及是否存在具名客户、供应商、竞争或互补关系。",
        "event": "截止前的一手公告、财报日历、事前预期和盘前价格反应。",
        "capital": "逐笔主动买卖、盘口深度、价差、连续时段和同期价格反应。",
        "derivatives": "期权隐含波动范围、偏斜、期限结构，以及能否确认谁主动和是否开新仓。",
        "price_volume": "历史价格路径、盘前相对行业的涨跌、成交参与和盘前价格区间。",
    }[domain]
    support = {
        "market": "市场内部方向并不一致，而且这些变化已经发生在盘前；现有证据不能判断开盘后延续还是反转。",
        "relationships": "没有确认具名商业关系及其传导方向，行业相近或历史同涨不能单独形成方向。",
        "event": "没有同时确认新事实、事前预期差、首次发布时间和盘前吸收程度，因此不能凭价格猜事件好坏。",
        "capital": "资金数据可用，但只有买卖压力、盘口承接和价格反应一致时才允许形成方向。",
        "derivatives": "期权可以说明市场预计波动多大；缺少主动方、开平仓和后续持仓确认时不能推断涨跌。",
        "price_volume": "当前价格路径同时存在延续和反转解释，单看缺口或技术指标不能确定开盘到收盘方向。",
    }[domain]
    counter = {
        "market": "如果盘前的市场与行业分化在开盘后继续，市场背景可能重新变成可用方向；若分化反转，影响也会相反。",
        "relationships": "若找到截止前有效的客户、供应商、竞争者或互补关系，同一事件可能沿真实业务关系传导。",
        "event": "盘前价格可能确实反映了好坏消息，但缺少一手结果时无法证明是哪一项事实造成。",
        "capital": "主动买卖分类可能受对冲、再平衡、盘口太薄或分类误差影响，短期压力也未必持续全天。",
        "derivatives": "Put、Call和偏斜也可能来自保护、组合、波动率交易或报价差异，而不是正股方向。",
        "price_volume": "同一个盘前缺口既可能是新信息尚未买完，也可能是价格已经走过头。",
    }[domain]
    unknowns = {
        "market": "开盘后的市场与行业是否继续，以及是否存在能维持到日内的统一催化。",
        "relationships": "是否存在截止前可核验的具名经济关系，以及消息沿这条关系传导的方向。",
        "event": "实际结果、指引、首次发布时间、可靠预期差和盘前已经消化的比例。",
        "capital": "有限盘口样本能否代表全市场，以及盘外、拍卖、对冲和成交分类误差的影响。",
        "derivatives": "谁主动成交、开仓还是平仓、是否为复杂组合，以及次日未平仓量是否确认。",
        "price_volume": "盘前变化的原因、开盘竞价承接和常规盘真实流动性。",
    }[domain]
    invalidation = {
        "market": "出现截止前可核验的共同催化，并能解释为何市场或行业冲击会延续到常规盘。",
        "relationships": "出现带时间和来源的具名商业关系，并能确定事件传导方向。",
        "event": "取得截止前发布的一手结果和事前预期，可以重新计算还未被价格吸收的信息。",
        "capital": "后续时段买卖压力转向，或资金方向不变但价格出现持续反向承接。",
        "derivatives": "取得可靠的主动买卖、开平仓语义，并由同一合约后续持仓变化确认。",
        "price_volume": "价格在成交参与扩大的同时稳定突破盘前区间，使延续或反转其中一种解释失效。",
    }[domain]

    if domain == "capital":
        analysis = _analysis_metrics(runtime, report, evidence_records)
        imbalance = analysis.get("signed_volume_imbalance")
        depth = analysis.get("signed_volume_to_median_visible_depth")
        reaction = analysis.get("corresponding_price_return")
        segments = analysis.get("segment_signed_imbalances", [])
        if imbalance is not None and depth is not None and reaction is not None:
            pressure = "主动卖出多于主动买入" if float(imbalance) < 0 else "主动买入多于主动卖出"
            segment_text = "三个连续时段方向一致" if segments and (all(float(x) < 0 for x in segments) or all(float(x) > 0 for x in segments)) else "连续时段方向不完全一致"
            reaction_text = "下跌" if float(reaction) < 0 else "上涨"
            support = (
                f"{pressure}{abs(float(imbalance)) * 100:.2f}个百分点；压力约为可见盘口中位深度的"
                f"{abs(float(depth)):.2f}倍；{segment_text}；同期价格{reaction_text}{abs(float(reaction)) * 100:.2f}%。"
            )
    elif domain == "derivatives":
        moves = re.findall(r"moves of ([0-9.]+)%.*?and ([0-9.]+)%", str(report.get("thesis", "")))
        if moves:
            support = f"最近两个期限隐含的双向波动约为{moves[0][0]}%和{moves[0][1]}%；但缺少可靠方向语义，所以保持中性。"
    elif domain == "price_volume":
        parts = [f"相对{metrics['sector_benchmark']}盘前多涨/少跌{_pct(metrics.get('residual'))}"]
        if metrics.get("volume") is not None:
            parts.append(f"盘前成交{_number(metrics['volume'])}股")
        if metrics.get("pre_low") is not None and metrics.get("pre_high") is not None:
            parts.append(f"盘前区间{float(metrics['pre_low']):.2f}–{float(metrics['pre_high']):.2f}")
        support = "；".join(parts) + "。这些数据说明冲击真实存在，但仍不能区分尚未走完和已经走过头。"

    if verdict == "bullish":
        support += " 该领域最终给出偏多判断。"
    elif verdict == "bearish":
        support += " 该领域最终给出偏空判断。"
    elif verdict == "not_applicable":
        support = "当天没有适用于这个领域的事件或关系，因此记为不适用，不算赞成也不算反对。"
    elif verdict == "unavailable":
        support = "采集器没有取得满足语义要求的数据，因此该领域不可用，不允许用替代指标凑方向。"

    source_hash = sha256_payload({
        "thesis": report.get("thesis"), "antithesis": report.get("antithesis"),
        "unknowns": report.get("unknowns"), "invalidation": report.get("invalidation"),
        "verdict": verdict, "evidence_ids": report.get("evidence_ids"),
    })
    return {
        "observed": observed, "support": support, "counter": counter,
        "unknowns": unknowns, "invalidation": invalidation, "source_hash": source_hash,
    }


def _gate_details(frozen: dict[str, Any], symbol: str) -> dict[str, Any]:
    reports = frozen.get("reports_by_symbol", {}).get(symbol, [])
    prediction = next((row for row in frozen.get("predictions", []) if row.get("symbol") == symbol), None)
    direction_counts = {
        direction: sum(row.get("verdict") == direction for row in reports)
        for direction in ("bullish", "bearish")
    }
    if prediction:
        direction = prediction["direction"]
    else:
        direction = max(direction_counts, key=lambda key: (direction_counts[key], key == "bearish"))
        if direction_counts[direction] == 0:
            direction = None
    aligned = [row for row in reports if direction and row.get("verdict") == direction]
    opposite = "bearish" if direction == "bullish" else "bullish"
    opposed = [row for row in reports if direction and row.get("verdict") == opposite]
    aligned_roots = {root for row in aligned for root in row.get("lineage_root_ids", [])}
    opposed_roots = {root for row in opposed for root in row.get("lineage_root_ids", [])}
    clean_aligned = aligned_roots - opposed_roots
    root_types = frozen.get("lineage", {}).get("root_component_types", {})
    context_types = {"market_context", "industry_context"}
    stock_types = {"stock_event", "stock_capital", "stock_derivatives", "stock_price_volume"}
    context_roots = {root for root in clean_aligned if set(root_types.get(root, [])) & context_types}
    stock_roots = {root for root in clean_aligned if set(root_types.get(root, [])) & stock_types}
    audit = frozen.get("integration_audit_by_symbol", {}).get(symbol, {})
    adversary = frozen.get("adversary_by_symbol", {}).get(symbol, {})
    return {
        "direction": direction,
        "domain_count": len(aligned),
        "root_count": len(clean_aligned),
        "context": bool(context_roots),
        "stock": bool(stock_roots),
        "opposed": len(opposed_roots - aligned_roots),
        "adversary_pass": adversary.get("veto") is not True,
        "published": bool(audit.get("published")),
        "reasons": audit.get("rejection_reasons", []),
    }


def _evidence_card(runtime: Path, evidence_id: str, record: dict[str, Any]) -> str:
    raw_link = _relative_href(runtime, record.get("raw_file_path"))
    analysis_link = _relative_href(runtime, record.get("analysis_file_path"))
    links = []
    if raw_link:
        links.append(f'<a href="{_escape(raw_link)}" target="_blank">原始文件</a>')
    if analysis_link:
        links.append(f'<a href="{_escape(analysis_link)}" target="_blank">计算结果</a>')
    return (
        '<div class="evidence-card">'
        f'<div><strong>{_escape(evidence_id)}</strong><span class="provider">{_escape(record.get("provider"))}</span></div>'
        f'<p>{_escape(record.get("source_uri"))}</p>'
        f'<p>抓取：{_escape(record.get("captured_at"))}</p>'
        f'<code>{_escape(record.get("raw_sha256"))}</code>'
        f'<div class="evidence-links">{" · ".join(links) if links else "文件入口不可用"}</div>'
        '</div>'
    )


def _domain_card(
    *, runtime: Path, symbol: str, report: dict[str, Any], candidate: dict[str, Any],
    metrics: dict[str, Any], evidence_records: dict[str, dict[str, Any]], ai_calls: dict[str, dict[str, Any]],
) -> str:
    domain = str(report["domain"])
    verdict = str(report["verdict"])
    plain = _plain_chinese(
        runtime=runtime, report=report, candidate=candidate, metrics=metrics,
        evidence_records=evidence_records,
    )
    evidence_html = "".join(
        _evidence_card(runtime, str(evidence_id), evidence_records.get(str(evidence_id), {}))
        for evidence_id in report.get("evidence_ids", [])
    ) or '<p class="muted">该领域没有引用证据。</p>'
    original_unknowns = "".join(f"<li>{_escape(value)}</li>" for value in report.get("unknowns", [])) or "<li>None</li>"
    original_invalidation = "".join(f"<li>{_escape(value)}</li>" for value in report.get("invalidation", [])) or "<li>None</li>"
    call = ai_calls.get(domain, {})
    audit = call.get("audit", {}) if isinstance(call, dict) else {}
    prompt_name = call.get("prompt_file", f"{domain}.prompt") if call else None
    packet_name = call.get("packet_file", f"{domain}.packet") if call else None
    prompt_link = _relative_href(runtime, runtime / "ai_calls" / str(prompt_name)) if prompt_name else None
    packet_link = _relative_href(runtime, runtime / "ai_calls" / str(packet_name)) if packet_name else None
    call_link = _relative_href(runtime, runtime / "ai_calls" / f"{domain}.json") if call else None
    audit_links = " · ".join(
        f'<a href="{_escape(link)}" target="_blank">{label}</a>'
        for label, link in (("完整提示词", prompt_link), ("冻结输入包", packet_link), ("原始AI输出", call_link)) if link
    )
    return f'''
    <section class="domain-card" data-domain="{_escape(domain)}">
      <header><div><span class="domain-index">{DOMAIN_ORDER.index(domain) + 1}</span><h3>{_escape(DOMAIN_ZH[domain])}</h3></div><span class="badge {verdict}">{_escape(VERDICT_ZH.get(verdict, verdict))}</span></header>
      <div class="plain-grid">
        <div><h4>它看到了什么</h4><p>{_escape(plain["observed"])}</p></div>
        <div><h4>为什么这样判断</h4><p>{_escape(plain["support"])}</p></div>
        <div><h4>最强反方</h4><p>{_escape(plain["counter"])}</p></div>
        <div><h4>仍然不知道</h4><p>{_escape(plain["unknowns"])}</p></div>
        <div class="wide"><h4>什么情况下需要推翻</h4><p>{_escape(plain["invalidation"])}</p></div>
      </div>
      <details class="original"><summary>查看AI英文原文与中文释义绑定</summary>
        <p class="hash-line">显示层来源哈希：<code>{_escape(plain["source_hash"])}</code></p>
        <h4>Thesis</h4><p>{_escape(report.get("thesis"))}</p>
        <h4>Antithesis</h4><p>{_escape(report.get("antithesis"))}</p>
        <h4>Unknowns</h4><ul>{original_unknowns}</ul>
        <h4>Invalidation</h4><ul>{original_invalidation}</ul>
      </details>
      <details><summary>查看证据来源（{len(report.get("evidence_ids", []))}项）</summary><div class="evidence-grid">{evidence_html}</div></details>
      <details><summary>查看本领域AI调用审计</summary>
        <div class="audit-facts"><span>模型：{_escape(audit.get("model"))}</span><span>推理：{_escape(audit.get("reasoning_effort"))}</span><span>开始：{_escape(audit.get("started_at_et"))}</span><span>完成：{_escape(audit.get("completed_at_et"))}</span></div>
        <p class="hash-line">输出哈希：<code>{_escape(audit.get("output_sha256"))}</code></p><p>{audit_links or "无AI调用文件"}</p>
      </details>
    </section>'''


def _gate_html(gate: dict[str, Any]) -> str:
    checks = [
        (gate["domain_count"] >= 2, f'同向领域 {gate["domain_count"]}/2'),
        (gate["root_count"] >= 2, f'独立证据根 {gate["root_count"]}/2'),
        (gate["context"], "市场或行业背景"),
        (gate["stock"], "个股自身证据"),
        (gate["opposed"] == 0, f'反向独立证据 {gate["opposed"]}/0'),
        (gate["adversary_pass"], "反方完整性检查"),
    ]
    items = "".join(
        f'<li class="{"pass" if passed else "fail"}"><span>{"✓" if passed else "×"}</span>{_escape(label)}</li>'
        for passed, label in checks
    )
    reasons = "、".join(REJECTION_ZH.get(str(value), str(value)) for value in gate.get("reasons", [])) or "全部门禁通过"
    direction = VERDICT_ZH.get(gate.get("direction"), "没有领域形成方向")
    final_text = "已发布预测" if gate["published"] else "未发布预测，不下单"
    return f'''
      <div class="gate-head"><div><span class="eyebrow">PYTHON程序裁决</span><h3>{_escape(direction)} → {_escape(final_text)}</h3></div><span class="badge {"bullish" if gate["published"] else "neutral"}">{"通过" if gate["published"] else "未通过"}</span></div>
      <ul class="gate-checks">{items}</ul><p class="gate-reason"><strong>最终原因：</strong>{_escape(reasons)}</p>'''


def _audit_failure_page(message: str) -> str:
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>运行回放审计失败</title><style>body{{font:16px/1.7 -apple-system,BlinkMacSystemFont,"Microsoft YaHei",sans-serif;background:#f6f8fb;color:#14213d;margin:0}}main{{max-width:760px;margin:12vh auto;padding:42px;background:#fff;border:1px solid #f1b5b5;border-radius:24px;box-shadow:0 18px 60px #24345218}}h1{{color:#b42318}}code{{overflow-wrap:anywhere}}</style></head><body data-audit-status="failed"><main><h1>这份回放没有通过完整性检查</h1><p>系统没有继续展示候选、AI结论或盘后结果，以免把被修改的数据误认为真实记录。</p><p><strong>检查结果：</strong>{_escape(message)}</p></main></body></html>'''


def run_replay(*, runtime: Path, frozen: dict[str, Any]) -> str:
    try:
        verification = verify_replay_inputs(runtime, frozen)
    except ReplayAuditError as exc:
        return _audit_failure_page(str(exc))

    intake = _read_json(runtime / "candidate_intake.json") or {"candidates": []}
    candidate_rows = [row for row in intake.get("candidates", []) if isinstance(row, dict) and row.get("symbol")]
    if not candidate_rows:
        candidate_rows = [{"symbol": symbol} for symbol in sorted(frozen.get("reports_by_symbol", {}))]
    candidate_by_symbol = {str(row["symbol"]): row for row in candidate_rows}
    audit_by_symbol = frozen.get("integration_audit_by_symbol", {})
    published = {row.get("symbol") for row in frozen.get("predictions", [])}
    symbols = sorted(
        candidate_by_symbol,
        key=lambda symbol: (
            0 if symbol in published else 1,
            -int(audit_by_symbol.get(symbol, {}).get("directional_domain_count", 0)),
            symbol,
        ),
    )
    default_symbol = symbols[0] if symbols else ""
    evidence_records = {
        str(record.get("evidence_id")): record
        for record in frozen.get("lineage", {}).get("records", []) if isinstance(record, dict)
    }
    ai_calls = {
        domain: (_read_json(runtime / "ai_calls" / f"{domain}.json") or {})
        for domain in DOMAIN_ORDER
    }
    adversary_call = _read_json(runtime / "ai_calls" / "adversary.json") or {}
    outcome_document = _read_json(runtime / "postmortem" / "outcomes_final.json")
    outcomes = outcome_document.get("rows_by_symbol", {}) if outcome_document else {}
    postmortem = _read_json(runtime / "postmortem" / "postmortem_final.json") or {}
    diagnostics = {row.get("symbol"): row for row in postmortem.get("candidate_diagnostics", []) if isinstance(row, dict)}
    broker = _read_json(runtime / "broker_journal.json") or {"orders": {}}
    orders = broker.get("orders", {})
    order_count = len(orders) if isinstance(orders, (dict, list)) else 0
    complete = _read_json(runtime / "audit_complete.json") or {}

    cards = []
    detail_sections = []
    for symbol in symbols:
        candidate = candidate_by_symbol[symbol]
        metrics = _candidate_metrics(runtime, candidate)
        gate = _gate_details(frozen, symbol)
        outcome = outcomes.get(symbol) if isinstance(outcomes, dict) else None
        outcome_html = (
            f'<span class="after-close">盘后 {_pct(outcome.get("official_open_to_close_return"))}</span>'
            if isinstance(outcome, dict) else ""
        )
        state = "已发布" if gate["published"] else "未入榜"
        cards.append(f'''
          <button class="candidate-card{" active" if symbol == default_symbol else ""}" data-symbol="{_escape(symbol)}" type="button" aria-pressed="{"true" if symbol == default_symbol else "false"}">
            <div class="candidate-card-top"><strong>{_escape(symbol)}</strong><span class="candidate-state">{state}</span></div>
            <div class="candidate-return">{_pct(metrics.get("stock_return"))}</div>
            <div class="candidate-meta"><span>相对{_escape(metrics["sector_benchmark"])} {_pct(metrics.get("residual"))}</span><span>方向领域 {gate["domain_count"]}</span></div>{outcome_html}
          </button>''')
        reports = {row["domain"]: row for row in frozen.get("reports_by_symbol", {}).get(symbol, [])}
        domain_cards = "".join(
            _domain_card(
                runtime=runtime, symbol=symbol, report=reports[domain], candidate=candidate,
                metrics=metrics, evidence_records=evidence_records, ai_calls=ai_calls,
            ) for domain in DOMAIN_ORDER if domain in reports
        )
        adversary = frozen.get("adversary_by_symbol", {}).get(symbol, {})
        adversary_audit = adversary_call.get("audit", {}) if isinstance(adversary_call, dict) else {}
        conflicts = "".join(f"<li>{_escape(value)}</li>" for value in adversary.get("unresolved_conflicts", [])) or "<li>没有未解决冲突</li>"
        adversary_link = _relative_href(runtime, runtime / "ai_calls" / "adversary.json")
        outcome_section = ""
        if isinstance(outcome, dict):
            diagnostic = diagnostics.get(symbol, {})
            attribution = diagnostic.get("attribution", {}) if isinstance(diagnostic, dict) else {}
            outcome_section = f'''
              <section class="post-close"><div class="section-heading"><div><span class="eyebrow">盘后核验｜盘前不可见</span><h3>{_escape(symbol)} 官方开盘到收盘</h3></div><span class="badge {outcome.get("actual_direction", "neutral")}">{_pct(outcome.get("official_open_to_close_return"))}</span></div>
                <div class="outcome-grid"><div><span>官方开盘</span><strong>${float(outcome.get("official_open")):.2f}</strong></div><div><span>官方收盘</span><strong>${float(outcome.get("official_close")):.2f}</strong></div><div><span>市场部分</span><strong>{_pct(attribution.get("market_component_return"))}</strong></div><div><span>行业部分</span><strong>{_pct(attribution.get("industry_component_return"))}</strong></div><div><span>个股部分</span><strong>{_pct(attribution.get("stock_specific_component_return"))}</strong></div></div>
                <p class="post-note">这部分在预测冻结后独立加入。{("系统没有发布预测，因此不记为命中。" if not gate["published"] else "该结果进入正式预测评价。")}</p>
              </section>'''
        detail_sections.append(f'''
          <article class="candidate-detail{" active" if symbol == default_symbol else ""}" data-detail-symbol="{_escape(symbol)}">
            <div class="candidate-hero"><div><span class="eyebrow">候选进入原因</span><h2>{_escape(symbol)}｜盘前 {_pct(metrics.get("stock_return"))}</h2><p>{_escape(candidate.get("gics_sector", "行业未知"))} · {metrics["sector_benchmark"]} {_pct(metrics.get("sector_return"))} · 相对行业 {_pct(metrics.get("residual"))} · 盘前成交 {_number(metrics.get("volume"))} 股</p></div><div class="hero-result"><span>最终状态</span><strong>{"已发布" if gate["published"] else "空榜"}</strong></div></div>
            <section><div class="section-heading"><div><span class="eyebrow">六个领域的可审计判断</span><h3>先看人话，再按需下钻原文和证据</h3></div></div><div class="domain-grid">{domain_cards}</div></section>
            <section class="adversary"><div class="section-heading"><div><span class="eyebrow">不计票的反方审查</span><h3>专门寻找遗漏、冲突和重复证据</h3></div><span class="badge {"bearish" if adversary.get("veto") else "neutral"}">{"否决" if adversary.get("veto") else "未否决"}</span></div>
              <p class="countercase">{_escape(adversary.get("strongest_countercase"))}</p><details><summary>查看未解决冲突与原始审计</summary><ul>{conflicts}</ul><p>模型：{_escape(adversary_audit.get("model"))} · 完成：{_escape(adversary_audit.get("completed_at_et"))}</p>{f'<a href="{_escape(adversary_link)}" target="_blank">打开反方原始AI输出</a>' if adversary_link else ''}</details>
            </section>
            <section class="gate">{_gate_html(gate)}</section>{outcome_section}
          </article>''')

    domain_audits = [value.get("audit", {}) for value in ai_calls.values() if value]
    ai_start = min((str(row.get("started_at_et")) for row in domain_audits if row.get("started_at_et")), default=None)
    ai_end = max((str(row.get("completed_at_et")) for row in domain_audits if row.get("completed_at_et")), default=None)
    adversary_audit = adversary_call.get("audit", {}) if isinstance(adversary_call, dict) else {}
    captured_at = outcome_document.get("captured_at_et") if outcome_document else None
    timeline = [
        ("收集并冻结数据", _time(frozen.get("cutoff_et")), "只允许截止时间前的资料"),
        ("六领域盲分析", f"{_time(ai_start)}–{_time(ai_end)}", "六次独立领域调用，不互看结论"),
        ("反方检查", _time(adversary_audit.get("completed_at_et")), "检查冲突、重复证据和完整性"),
        ("程序门禁", _time(frozen.get("created_at")), "Python按固定规则裁决"),
        ("预测冻结", _time(frozen.get("created_at")), f"{len(frozen.get('predictions', []))}只正式预测"),
        ("模拟订单", str(broker.get("run_status", "未提交")), f"{order_count}条订单"),
        ("盘后核验", _time(captured_at) if captured_at else "尚未生成", "与盘前区域物理分开"),
    ]
    timeline_html = "".join(f'<li><span class="dot"></span><strong>{_escape(title)}</strong><time>{_escape(moment)}</time><p>{_escape(note)}</p></li>' for title, moment, note in timeline)
    trade_date = str(frozen.get("run_id", "")).replace("SHAQ-CANARY-", "").split("-001")[0]
    verification_label = "审计通过" if verification["status"] == "passed" else f'审计{verification["status"]}'
    report_count = len(frozen.get("reports_by_symbol", {})) * len(DOMAIN_ORDER)
    stylesheet = _REPLAY_CSS
    script = _REPLAY_JS
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SHAQ Daily Oracle｜{_escape(trade_date)}运行回放</title><style>{stylesheet}</style></head>
<body data-audit-status="passed" data-default-symbol="{_escape(default_symbol)}"><main>
  <header class="top"><div><span class="kicker">SHAQ DAILY ORACLE · 真实运行回放</span><h1>{_escape(trade_date)}｜从盘前证据到最终空榜</h1><p>这不是重新讲故事。页面只读取当日冻结文件，并把机器记录翻译成可以逐层核验的工作过程。</p></div><div class="audit-seal"><span>✓</span><strong>{_escape(verification_label)}</strong><small>{verification["verified_evidence_count"]}份证据重新校验</small></div></header>
  <section class="summary-strip"><div><span>运行模式</span><strong>{"正式模拟" if frozen.get("mode") == "canary" else "影子记录"}</strong></div><div><span>证据截止</span><strong>{_escape(_time(frozen.get("cutoff_et")))}</strong></div><div><span>候选</span><strong>{len(symbols)}只</strong></div><div><span>六域报告</span><strong>{report_count}份</strong></div><div><span>正式预测</span><strong>{len(frozen.get("predictions", []))}只</strong></div><div><span>模拟订单</span><strong>{order_count}条</strong></div></section>
  <details class="run-hash"><summary>查看运行身份与校验值</summary><p>运行编号：<code>{_escape(frozen.get("run_id"))}</code></p><p>系统身份：<code>{_escape(frozen.get("system_identity"))}</code></p><p>冻结运行SHA-256：<code>{_escape(frozen.get("run_sha256"))}</code></p></details>
  <section class="timeline-section"><div class="section-heading"><div><span class="eyebrow">全过程</span><h2>一条不能事后改写的时间线</h2></div></div><ol class="timeline">{timeline_html}</ol></section>
  <section class="candidates"><div class="section-heading"><div><span class="eyebrow">当天完整候选池</span><h2>先选择股票，再查看它的六领域证据链</h2></div><p>默认展开方向领域最多的候选；同分按代码排序。</p></div><div class="candidate-grid">{"".join(cards)}</div></section>
  <div id="candidate-details">{"".join(detail_sections)}</div>
  <footer><p>机器可读记录仍保留在相邻JSON文件中；本页只是经过哈希绑定的可视化入口，不参与预测，也不能修改冻结结果。</p></footer>
</main><script>{script}</script></body></html>'''


def write_run_replay(*, runtime: Path, frozen: dict[str, Any]) -> dict[str, Any]:
    page = run_replay(runtime=runtime, frozen=frozen)
    output = runtime / "run_replay.html"
    output.write_text(page, encoding="utf-8")
    status = "passed" if 'data-audit-status="passed"' in page else "failed"
    manifest = {
        "schema_version": 1,
        "status": status,
        "frozen_run_sha256": frozen.get("run_sha256"),
        "run_replay_sha256": sha256_file(output),
    }
    manifest["manifest_sha256"] = sha256_payload(manifest)
    (runtime / "run_replay_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


_REPLAY_CSS = r'''
:root{--ink:#0d2948;--muted:#607086;--blue:#1677d2;--blue2:#eaf4ff;--green:#108a68;--green2:#e9f8f2;--red:#c23d42;--red2:#fff0f0;--amber:#c87900;--amber2:#fff7e8;--line:#dbe5ee;--paper:#fff;--bg:#f3f7fb}*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.65 -apple-system,BlinkMacSystemFont,"Microsoft YaHei","Noto Sans CJK SC",Arial,sans-serif}button{font:inherit}main{max-width:1440px;margin:0 auto;padding:38px 44px 60px}.top{display:grid;grid-template-columns:1fr auto;gap:28px;align-items:center;background:linear-gradient(135deg,#0c2b4c,#174f80);color:#fff;border-radius:28px;padding:38px 42px;box-shadow:0 18px 50px #173e6226}.kicker,.eyebrow{display:block;font-size:12px;font-weight:800;letter-spacing:.13em;text-transform:uppercase;color:#58b8ff}.top h1{font-size:36px;line-height:1.22;margin:8px 0 10px}.top p{margin:0;color:#d9e8f5;max-width:800px}.audit-seal{min-width:170px;text-align:center;padding:18px 22px;background:#ffffff12;border:1px solid #ffffff30;border-radius:20px}.audit-seal span{display:block;width:42px;height:42px;border-radius:50%;background:#53d6a5;color:#083627;font-size:25px;line-height:42px;margin:0 auto 8px}.audit-seal strong,.audit-seal small{display:block}.audit-seal small{color:#cde2f3}.summary-strip{display:grid;grid-template-columns:repeat(6,1fr);gap:1px;margin:22px 0;background:var(--line);border:1px solid var(--line);border-radius:18px;overflow:hidden}.summary-strip div{background:#fff;padding:18px 20px}.summary-strip span,.outcome-grid span{display:block;color:var(--muted);font-size:12px}.summary-strip strong{font-size:20px}.run-hash{background:#fff;border:1px solid var(--line);border-radius:14px;padding:12px 18px}.run-hash summary,details summary{cursor:pointer;font-weight:750}.run-hash code,.hash-line code,.evidence-card code{overflow-wrap:anywhere;font-size:11px}.section-heading{display:flex;align-items:end;justify-content:space-between;gap:20px;margin:46px 0 18px}.section-heading h2,.section-heading h3{margin:4px 0 0}.section-heading h2{font-size:28px}.section-heading p{color:var(--muted);margin:0}.timeline{display:grid;grid-template-columns:repeat(7,1fr);padding:0;margin:0;list-style:none;background:#fff;border:1px solid var(--line);border-radius:20px;overflow:hidden}.timeline li{position:relative;padding:24px 18px;border-right:1px solid var(--line);min-height:150px}.timeline li:last-child{border-right:0}.timeline .dot{display:block;width:11px;height:11px;border-radius:50%;background:var(--blue);box-shadow:0 0 0 5px var(--blue2);margin-bottom:17px}.timeline strong,.timeline time{display:block}.timeline time{font-size:12px;color:var(--blue);margin:4px 0}.timeline p{font-size:12px;color:var(--muted);margin:0}.candidate-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.candidate-card{text-align:left;background:#fff;border:1px solid var(--line);border-radius:18px;padding:18px;cursor:pointer;color:var(--ink);transition:.16s transform,.16s border,.16s box-shadow}.candidate-card:hover{transform:translateY(-2px);box-shadow:0 12px 28px #12365713}.candidate-card.active{border:2px solid var(--blue);background:var(--blue2);box-shadow:0 12px 32px #1677d21b}.candidate-card-top{display:flex;justify-content:space-between;align-items:center}.candidate-card-top strong{font-size:20px}.candidate-state{font-size:12px;color:var(--muted)}.candidate-return{font-size:30px;font-weight:850;margin:9px 0 4px}.candidate-meta{display:flex;justify-content:space-between;gap:12px;font-size:12px;color:var(--muted)}.after-close{display:inline-block;margin-top:10px;padding:3px 8px;border-radius:99px;background:#eef1f5;color:#566273;font-size:11px}.candidate-detail{display:none}.candidate-detail.active{display:block}.candidate-hero{display:flex;justify-content:space-between;align-items:center;background:#fff;border:1px solid var(--line);border-radius:22px;padding:28px 30px;margin-top:24px}.candidate-hero h2{font-size:30px;margin:3px 0}.candidate-hero p{color:var(--muted);margin:0}.hero-result{text-align:center;background:var(--red2);color:var(--red);padding:15px 24px;border-radius:16px}.hero-result span,.hero-result strong{display:block}.hero-result strong{font-size:24px}.domain-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:16px}.domain-card{background:#fff;border:1px solid var(--line);border-radius:20px;padding:22px;min-width:0}.domain-card>header{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}.domain-card>header>div{display:flex;align-items:center;gap:10px}.domain-card h3{margin:0;font-size:20px}.domain-index{width:28px;height:28px;border-radius:9px;background:var(--blue2);color:var(--blue);display:grid;place-items:center;font-weight:800}.badge{display:inline-block;padding:5px 11px;border-radius:99px;font-size:12px;font-weight:800}.badge.bullish{background:var(--green2);color:var(--green)}.badge.bearish{background:var(--red2);color:var(--red)}.badge.neutral,.badge.not_applicable,.badge.unavailable{background:#eef2f6;color:#647086}.plain-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.plain-grid>div{background:#f7f9fc;border-radius:13px;padding:12px 14px}.plain-grid .wide{grid-column:1/-1}.plain-grid h4,.original h4{font-size:12px;color:var(--blue);margin:0 0 4px}.plain-grid p{margin:0}.domain-card details,.adversary details{border-top:1px solid var(--line);margin-top:14px;padding-top:12px}.domain-card details p,.domain-card details li{color:#45586e}.evidence-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}.evidence-card{background:#f7f9fc;border:1px solid var(--line);border-radius:12px;padding:12px;min-width:0}.evidence-card div:first-child{display:flex;justify-content:space-between;gap:8px}.evidence-card p{font-size:11px;margin:5px 0;overflow-wrap:anywhere}.provider{font-size:10px;color:var(--muted)}.evidence-links{font-size:12px;margin-top:8px}.evidence-links a,details a{color:var(--blue)}.audit-facts{display:flex;gap:10px;flex-wrap:wrap;margin-top:10px}.audit-facts span{background:#f3f6fa;padding:5px 8px;border-radius:8px;font-size:11px}.adversary,.gate,.post-close{background:#fff;border:1px solid var(--line);border-radius:22px;padding:24px 28px;margin-top:18px}.adversary{border-left:5px solid var(--amber)}.adversary .section-heading,.gate .section-heading,.post-close .section-heading{margin:0 0 14px}.countercase{font-size:17px}.gate{border-left:5px solid var(--blue)}.gate-head{display:flex;justify-content:space-between;align-items:center}.gate-head h3{margin:4px 0 0}.gate-checks{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;list-style:none;padding:0;margin:18px 0}.gate-checks li{padding:11px 13px;border-radius:11px;background:#f7f9fc}.gate-checks li span{display:inline-grid;place-items:center;width:21px;height:21px;border-radius:50%;margin-right:7px;font-weight:900}.gate-checks .pass span{background:var(--green2);color:var(--green)}.gate-checks .fail span{background:var(--red2);color:var(--red)}.gate-reason{background:var(--red2);padding:11px 14px;border-radius:11px;margin:0}.post-close{border:2px dashed #9da8b5;background:#f8f9fb}.post-close .eyebrow{color:#687789}.outcome-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}.outcome-grid div{background:#fff;border-radius:12px;padding:12px}.outcome-grid strong{font-size:19px}.post-note{margin:14px 0 0;color:#5c6878}footer{margin-top:40px;padding:22px;text-align:center;color:var(--muted);border-top:1px solid var(--line)}@media(max-width:1000px){main{padding:24px}.summary-strip{grid-template-columns:repeat(3,1fr)}.timeline{grid-template-columns:repeat(2,1fr)}.timeline li{border-bottom:1px solid var(--line)}.candidate-grid{grid-template-columns:repeat(2,1fr)}.domain-grid{grid-template-columns:1fr}.gate-checks{grid-template-columns:repeat(2,1fr)}}@media(max-width:620px){main{padding:12px}.top{grid-template-columns:1fr;padding:26px 22px}.top h1{font-size:28px}.audit-seal{display:flex;gap:10px;align-items:center;text-align:left}.audit-seal span{margin:0}.summary-strip{grid-template-columns:repeat(2,1fr)}.timeline{grid-template-columns:1fr}.candidate-grid{grid-template-columns:1fr}.candidate-hero{align-items:flex-start;gap:18px}.candidate-hero h2{font-size:25px}.plain-grid{grid-template-columns:1fr}.plain-grid .wide{grid-column:auto}.evidence-grid{grid-template-columns:1fr}.gate-checks{grid-template-columns:1fr}.outcome-grid{grid-template-columns:repeat(2,1fr)}.section-heading{align-items:flex-start;flex-direction:column}.candidate-meta{flex-direction:column}.hero-result{padding:10px 14px}}
'''


_REPLAY_JS = r'''
(()=>{const buttons=[...document.querySelectorAll('.candidate-card')];const panels=[...document.querySelectorAll('.candidate-detail')];function select(symbol){buttons.forEach(button=>{const active=button.dataset.symbol===symbol;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active))});panels.forEach(panel=>panel.classList.toggle('active',panel.dataset.detailSymbol===symbol));history.replaceState(null,'','#'+symbol)}buttons.forEach(button=>button.addEventListener('click',()=>select(button.dataset.symbol)));const requested=decodeURIComponent(location.hash.slice(1));if(requested&&buttons.some(button=>button.dataset.symbol===requested))select(requested)})();
'''
