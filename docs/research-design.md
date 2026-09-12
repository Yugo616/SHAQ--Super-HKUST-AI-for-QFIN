# Research and comparison design

SHAQ Daily Oracle Lab studies how governed analysis methods behave on the same point-in-time financial evidence. It is designed for prospective comparison and auditable abstention, not for claiming a profitable strategy from a small replay.

## Question and target

For each US trading date, the target is the direction of the official unadjusted regular-session open-to-close return. Evidence is frozen before the configured cutoff. A method may publish zero to three bullish or bearish directions; unavailable and neutral domain conclusions remain explicit.

The research unit is a frozen batch:

```text
versioned universe and source observations
                  ↓
 timestamps + files + SHA-256 + PROV lineage
                  ↓
 candidates and one frozen evidence packet
                  ↓
 six isolated domain reports
                  ↓
 non-voting integrity adversary
                  ↓
 method-specific validated decision
                  ↓
 immutable local record
                  ↓
 later label and brokerless minute replay
```

Domain analyzers do not see later labels, other domain reports, or a ranking-derived target. The adversary can identify duplicate lineage, conflicting evidence, a horizon mismatch, unsupported facts, or a cutoff failure. It cannot browse, add evidence, change a domain verdict, or count as a vote. Deterministic code validates schemas, citations, time, lineage, sandbox output, and the three-direction cap.

## The two bundled methods

### Independent Evidence Gate · Formal Baseline

The baseline makes the publication decision in deterministic JavaScript. A direction must have at least two applicable aligned domains and two unconflicted evidence roots, including one market/industry root and one stock-specific root. Any independent opposing root prevents publication. An integrity veto also prevents publication. Eligible candidates are ordered deterministically and capped at three.

### Cross-domain Synthesis · Shadow

Both methods receive the same frozen evidence. Each method produces its own validated domain reports and adversary record; identical unchanged tasks can reuse a valid cached result. The Shadow then asks a separate synthesis step to compare the leading mechanism with the strongest countercase and candidate alternatives. Each output includes thesis, antithesis, resolution, comparison, unknowns, invalidation conditions, and supplied evidence IDs. Program code validates those references, integrity constraints, and the same maximum of three.

This is a method contrast, not a claim that synthesis is more intelligent or that the baseline is more profitable. The labels **Formal Baseline** and **Shadow** identify experimental roles only.

## When a comparison is controlled

The results view compares actual frozen identities rather than labels or author names.

| Dimension | Frozen identity used |
|---|---|
| Method | SHA-256 of the saved method documents |
| Model | Saved model-profile SHA-256, request-policy identity, and recorded response model; unresolved defaults remain unknown |
| Data | Frozen evidence-manifest hash |
| Candidates | SHA-256 of the actual candidate array |
| Trading rules | Execution-policy hash together with engine and engine version |
| Date | Trade date derived from the frozen `as_of` timestamp |

A method-effect interpretation requires model, data, candidates, trading rules, and date to be the same, with both method snapshots present. If the method also matches, the pair is a repeatability check rather than a method contrast. If any non-method dimension is different, the UI explains that the pair is observational but not a pure method comparison. If either value is absent, the dimension is **Unknown**; missing historical metadata never becomes assumed equality.

The comparison preserves the rows even when inputs differ. It shows method-file differences, published or rejected stock outcomes, decision reasons, and after-close replay status. It does not fabricate a matched pair or infer an omitted identity from a display name.

## Evaluation boundaries

- Official direction correctness uses the unadjusted regular-session open and close.
- Costed virtual-account profit/loss uses independently captured target minutes, integer shares, stated commission, and stated adverse slippage.
- A same-share zero-cost replay is a reference, not an account balance.
- Prospective, historical, practice, late, and duplicate scopes remain separate.
- A provisional observation is not presented as independently confirmed.
- Missing minute evidence produces a waiting, unavailable, or incomplete state; it is not imputed.
- One run, a selected date range, or a replay cannot establish statistical or economic superiority.

Coverage, abstention, valid-output rate, cost, latency, and prospective outcomes all matter. Direction accuracy alone omits both selection and execution effects. See [Paper evaluation](paper-evaluation.md) for the current replay policy.

## Eight research Skills and foundations

| Skill | Research role | Selected foundation |
|---|---|---|
| `daily-oracle` | Orchestrates cutoff, freezing, lineage, reports, decision, and later evaluation | [W3C PROV-DM](https://www.w3.org/TR/prov-dm/) for provenance; [Gangrade et al.](https://proceedings.mlr.press/v130/gangrade21a.html) for coverage-risk evaluation |
| `market-common-shock` | Separates common market mechanisms from stock-specific effects and overnight realization | [Cieslak and Pang](https://doi.org/10.1016/j.jfineco.2021.06.008); [Lou, Polk and Skouras](https://doi.org/10.1016/j.jfineco.2019.03.011) |
| `pit-peer-spillover` | Tests named, point-in-time customer, supplier, competitor, complement, and industry links | [Hoberg and Phillips](https://doi.org/10.1086/688176); [Cohen and Frazzini](https://doi.org/10.1111/j.1540-6261.2008.01379.x) |
| `primary-event-reasoner` | Separates new primary-source facts, prior expectations, publication time, and price absorption | [Jiang, Li and Wang](https://doi.org/10.1016/j.jfineco.2021.04.003); [SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) |
| `capital-order-flow` | Requires signed event-level flow and liquidity context rather than vendor size labels | [Cont, Kukanov and Stoikov](https://doi.org/10.1093/jjfinec/nbt003) |
| `derivatives-evidence` | Reads implied distributions and accepts direction only with reliable initiation/position semantics | [Pan and Poteshman](https://doi.org/10.1093/rfs/hhj024); [Cremers and Weinbaum](https://doi.org/10.1017/S002210901000013X) |
| `price-volume-structure` | Classifies residual path, participation, liquidity, continuation, and reversal mechanisms | [Murray, Xia and Xiao](https://doi.org/10.1016/j.jfineco.2024.103791); [Sullivan, Timmermann and White](https://doi.org/10.1111/0022-1082.00163) |
| `thesis-adversary` | Checks countercases, leakage, duplicate evidence, and horizon mismatch without voting | [FinCon](https://proceedings.neurips.cc/paper_files/paper/2024/hash/f7ae4fe91d96f50abc2211f09b6a7e49-Abstract-Conference.html); [InvestorBench](https://aclanthology.org/2025.acl-long.126/) |

The full reference lists and domain-specific decision boundaries live with each versioned Skill, so a saved method snapshot retains the sources that governed it.

## TradingAgents: fixed reference and deliberate differences

TradingAgents is an external research reference, not a SHAQ runtime dependency. This comparison is pinned to commit [`be952b8eccb49720509af544c6675233bc1f10d0`](https://github.com/TauricResearch/TradingAgents/commit/be952b8eccb49720509af544c6675233bc1f10d0), whose repository license is [Apache License 2.0](https://github.com/TauricResearch/TradingAgents/blob/be952b8eccb49720509af544c6675233bc1f10d0/LICENSE). No claim below applies automatically to later commits.

| Upstream mechanism at the pinned commit | SHAQ treatment |
|---|---|
| Declarative [model capability records](https://github.com/TauricResearch/TradingAgents/blob/be952b8eccb49720509af544c6675233bc1f10d0/tradingagents/llm_clients/capabilities.py) and provider-specific parameter handling | **Adapted.** SHAQ keeps official APIs, compatible relays, and local subscriptions distinct, probes a real structured response, and fails without silently changing provider. It does not infer every protocol rule from a model-name prefix. |
| Per-run [checkpoint recovery](https://github.com/TauricResearch/TradingAgents/blob/be952b8eccb49720509af544c6675233bc1f10d0/tradingagents/graph/checkpointer.py) and progress records | **Adopted in a stricter identity boundary.** SHAQ recovery is bound to the frozen batch, method, model, schema, and task; completed valid calls are reused and unfinished work remains visible. |
| Deterministic [market numeric snapshot validation](https://github.com/TauricResearch/TradingAgents/blob/be952b8eccb49720509af544c6675233bc1f10d0/tradingagents/dataflows/market_data_validator.py) | **Adapted.** SHAQ freezes source rows before analysis and validates timestamps, hashes, and lineage in Python; instructions alone cannot turn an unsupported number into evidence. |
| Explicit [analyst execution plan](https://github.com/TauricResearch/TradingAgents/blob/be952b8eccb49720509af544c6675233bc1f10d0/tradingagents/graph/analyst_execution.py), structured reports, debate, and manager decision | **Adapted, not copied.** SHAQ keeps six evidence domains isolated, uses one non-voting adversary, and separates language analysis from deterministic integration. It does not add roles merely to simulate a trading firm. |
| Durable decision records and delayed realized-return [memory/reflection](https://github.com/TauricResearch/TradingAgents/blob/be952b8eccb49720509af544c6675233bc1f10d0/tradingagents/agents/utils/memory.py) | **Records adopted; automatic historical reflection excluded from the official SHAQ input path.** SHAQ freezes predictions, later attaches labels/replay, and keeps post-close review separate so an outcome cannot rewrite the original method or official record. |

The pinned TradingAgents version has multi-provider/model support, checkpoint history, and a decision log; SHAQ does not claim those capabilities are absent upstream. Conversely, SHAQ's stricter freezing and comparison identities are design choices, not evidence of higher returns or universal superiority.

## Known limitations

Free/public sources have entitlement, retention, timestamp, and semantic limits. Current classification metadata cannot prove historical index membership. Basic option chains do not reveal who initiated or opened a trade. Missing order-book/aggressor semantics makes capital flow unavailable. Language-model output remains non-deterministic, and provider behavior can change. The workbench records these limits; it does not remove them.
