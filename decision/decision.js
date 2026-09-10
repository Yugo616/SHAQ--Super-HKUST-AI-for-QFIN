function decide(input) {
  const contextTypes = new Set(["market_context", "industry_context"]);
  const stockTypes = new Set([
    "stock_event",
    "stock_capital",
    "stock_derivatives",
    "stock_price_volume",
  ]);
  const candidates = new Map(input.candidates.map((candidate) => [candidate.symbol, candidate]));
  const eligible = [];
  const rejected = [];

  for (const symbol of [...candidates.keys()].sort()) {
    const adversary = input.adversary_by_symbol[symbol];
    if (adversary.veto) {
      rejected.push({symbol, action: "reject", direction: "neutral", reason: "反方完整性审查否决", evidence_root_ids: []});
      continue;
    }
    const reports = input.reports_by_symbol[symbol];
    const outcomes = [];
    for (const direction of ["bullish", "bearish"]) {
      const opposite = direction === "bullish" ? "bearish" : "bullish";
      const aligned = new Set();
      const opposed = new Set();
      const domains = new Set();
      for (const report of reports) {
        if (report.availability !== "available") continue;
        if (report.verdict === direction) {
          report.lineage_root_ids.forEach((root) => aligned.add(root));
          if (report.lineage_root_ids.length) domains.add(report.domain);
        } else if (report.verdict === opposite) {
          report.lineage_root_ids.forEach((root) => opposed.add(root));
        }
      }
      const conflict = new Set([...aligned].filter((root) => opposed.has(root)));
      const cleanAligned = [...aligned].filter((root) => !conflict.has(root));
      const cleanOpposed = [...opposed].filter((root) => !conflict.has(root));
      const hasContext = cleanAligned.some((root) =>
        (input.root_component_types[root] || []).some((type) => contextTypes.has(type))
      );
      const hasStock = cleanAligned.some((root) =>
        (input.root_component_types[root] || []).some((type) => stockTypes.has(type))
      );
      if (cleanAligned.length >= 2 && domains.size >= 2 && cleanOpposed.length === 0 && hasContext && hasStock) {
        outcomes.push({direction, roots: cleanAligned.sort()});
      }
    }
    if (outcomes.length === 1) {
      eligible.push({symbol, outcome: outcomes[0]});
    } else {
      rejected.push({
        symbol,
        action: "reject",
        direction: "neutral",
        reason: outcomes.length ? "多空条件同时成立，方向无法区分" : "未满足两个独立领域及市场与个股证据门禁",
        evidence_root_ids: [],
      });
    }
  }

  eligible.sort((left, right) => right.outcome.roots.length - left.outcome.roots.length || left.symbol.localeCompare(right.symbol));
  const selected = new Set(eligible.slice(0, 3).map((row) => row.symbol));
  const decisions = [...rejected];
  for (const row of eligible) {
    decisions.push(selected.has(row.symbol) ? {
      symbol: row.symbol,
      action: "publish",
      direction: row.outcome.direction,
      reason: "两个独立领域同向，且同时包含市场或行业背景与个股证据",
      evidence_root_ids: row.outcome.roots,
    } : {
      symbol: row.symbol,
      action: "reject",
      direction: "neutral",
      reason: "通过门禁但未进入最多三只的发布上限",
      evidence_root_ids: [],
    });
  }
  decisions.sort((left, right) => left.symbol.localeCompare(right.symbol));
  return {schema_version: 1, decisions};
}
