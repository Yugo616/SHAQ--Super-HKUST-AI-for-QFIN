---
name: market-common-shock
description: Diagnose premarket common shocks across equities, rates, dollar, credit and volatility, separate already-realized overnight moves from expected regular-session impact, and produce an evidence-linked market DomainReport. Use for the market domain of Daily Oracle or when a stock move may be explained by beta, macro news, discount rates or risk premia.
---

# Market Common Shock

## Method

1. Start from timestamped index futures or ETFs, Treasury curve, dollar, credit and volatility evidence.
2. Classify the candidate mechanism: expected cash flow, real-rate/discount rate, risk premium, liquidity, or unresolved mixture.
3. Separate `previous close -> premarket` realization from the requested `official open -> close` horizon.
4. Compare broad, size and style baskets. A single risk-on/risk-off label is insufficient.
5. State why the common shock should persist, reverse or cease at the open.
6. Write the strongest contradiction, especially cross-asset disagreement or a fully absorbed gap.

## Abstain

Return `unavailable` when timestamps, previous-close semantics or cross-asset snapshots cannot be verified. Return `neutral` when evidence describes volatility but not direction.

## Output

Use the common DomainReport contract. The thesis must name the mechanism and distinguish beta background from stock-specific direction. Do not emit probabilities, scores or other-domain conclusions.

Read [foundations](references/foundations.md).
