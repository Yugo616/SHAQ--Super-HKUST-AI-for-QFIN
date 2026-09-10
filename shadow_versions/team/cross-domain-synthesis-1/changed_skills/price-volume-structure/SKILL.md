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

Return `provider_error + unavailable` for broken session semantics or adjusted/unadjusted mixing. If premarket volume maturity is missing, do not use that volume: retain valid T-1 price-path evidence and explicitly limit the claim. Return `available + neutral` when valid observations do not favor a conditional mechanism; use `no_data + unavailable` only when no usable path evidence remains.

## Prohibited shortcuts

Do not use a universal golden/death cross, RSI threshold, fixed stop, fixed profit target, “gap always follows,” or “gap always fills.” These may be separately registered for a specific mechanism but are not SHAQ rules.

## Output

Use the DomainReport contract with `component_type=price_volume_state`. The verdict is the residual price-volume contribution to absolute return. Describe state and mechanism in plain language; do not emit indicator votes or confidence scores.

Read [foundations](references/foundations.md).


## Component scope

Report only the effect supported by this domain. It need not independently establish the whole-stock daily forecast. Describe known observations, the conditional mechanism and missing measurements separately. An unobserved future open is not a prerequisite for premarket analysis. Do not substitute assumptions for missing data or turn a realized gap into a forecast.
