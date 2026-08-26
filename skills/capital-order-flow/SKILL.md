---
name: capital-order-flow
description: Analyze liquidity-adjusted order-flow imbalance, depth, spread and event-level trade direction without equating vendor large-order labels with informed money. Use for the capital or microstructure domain of Daily Oracle and for claims about buying pressure, selling pressure, accumulation or distribution.
---

# Capital Order Flow

## Method

1. Verify event-level trades or quotes, aggressor classification, bid/ask depth, spread and timestamps.
2. Compute signed order-flow imbalance at the relevant sampling interval.
3. Normalize by local depth and liquidity; a dollar total without capacity is not comparable.
4. Check persistence across intervals and whether price impact confirms or reverses the flow.
5. Separate auction, off-exchange, block and regular continuous trading when identifiable.
6. Treat vendor “main force” or large-order buckets as descriptive raw fields only.
7. State the strongest microstructure alternative: hedging, rebalancing, thin depth or classification error.

## Abstain

Return `unavailable` without reliable order book, aggressor or event-level semantics. No same-day premarket trades is `no_data`, not a provider failure. Do not substitute aggregate money flow, volume, short volume or broker rankings for OFI.

## Output

Use the DomainReport contract with `component_type=capital_flow`. A directional verdict must identify measured imbalance, liquidity normalization, persistence and corresponding price reaction. Otherwise report `unavailable` or `neutral`.

Read [foundations](references/foundations.md).
