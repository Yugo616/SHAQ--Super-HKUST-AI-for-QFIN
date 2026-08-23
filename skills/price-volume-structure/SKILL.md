---
name: price-volume-structure
description: Classify nonlinear price-path and participation states for the open-to-close horizon using residual gaps, event context, volume and liquidity, without universal chart rules. Use for Daily Oracle price-volume analysis, gap continuation versus reversal, trend exhaustion or technical-structure questions.
---

# Price-Volume Structure

## Method

1. Use unadjusted OHLCV with explicit session boundaries and corporate-action checks.
2. Separate market/peer components to obtain residual path and residual gap.
3. Represent the recent path as a state: trend, compression, shock, post-event digestion, failed breakout or unresolved mixture.
4. Combine path with participation, relative volume, spread/liquidity and event type.
5. Ask whether the opening inventory is likely under- or over-cleared for the requested horizon.
6. Compare continuation and reversal mechanisms; neither a gap nor an indicator has a fixed sign.
7. State the observable structure that would invalidate the premarket thesis.

## Abstain

Return `unavailable` for broken session semantics, adjusted/unadjusted mixing or missing volume maturity. Return `neutral` when a chart pattern lacks event or participation context.

## Prohibited shortcuts

Do not use a universal golden/death cross, RSI threshold, fixed stop, fixed profit target, “gap always follows,” or “gap always fills.” These may be separately registered for a specific mechanism but are not SHAQ rules.

## Output

Use the DomainReport contract. Describe state and mechanism in plain language; do not emit indicator votes or confidence scores.

Read [foundations](references/foundations.md).
