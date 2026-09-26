---
name: market-common-shock
description: Diagnose premarket common shocks across equities, rates, the dollar, credit and volatility, separate the already-realized overnight move from the expected regular-session impact, and produce an evidence-linked market DomainReport. Use for the market domain of Daily Oracle or when a stock move may be explained by beta, macro news, discount rates or risk premia.
---

# Market Common Shock

You always receive premarket snapshots for the fixed benchmark set: broad, size and style equity; rates; dollar; credit; volatility; and the eleven GICS sector ETFs.
What you receive about the candidate itself varies by runtime, and you must not assume which: either a point-in-time relationship view (sector, multi-ETF betas, beta-stability checks, residual volatility) with no price history for the stock or the benchmarks, or recent daily and premarket history for the stock and every benchmark with no sector, beta, stability or residual-volatility statistic.
You never see order flow, options, filings, a macro calendar, or the official outcome. Judge only the market-beta contribution to the coming official open-to-close move.

## Method

1. Inventory usable benchmarks: use an ETF only when its premarket reading is usable — `premarket_semantics.status` is `pass` in a snapshot record, or the premarket `status` is `collected` in a record with daily history — and read its signed `premarket_return`.
   Use the premarket range (`raw_snapshot.pre_amplitude`, or the high-low span of the premarket bars) as a same-session cue, and premarket volume (`pre_volume` or `observed_volume`) only as raw participation: neither runtime gives a baseline for premarket volume.
   Record every proxy that is absent, and treat every usable reading as noisier than a regular-session price, the more so the more thinly that ETF trades before the open.

2. Take the cross-asset sign vector: broad, style and size equity (SPY, QQQ, IWM), rates via IEF (IEF up means Treasury yields down), the dollar (UUP), credit (HYG) and volatility (VIXY).

3. Classify the quadrant from the joint sign of equities and Treasury yields, the pattern by which the four common shocks are identified on full-day moves; your premarket leg is a partial reading of it.
   Each quadrant holds two candidates that only the maturity profile of the rate move can separate, and IEF alone gives no maturity profile:
   equities up with yields up is good growth (cash-flow) news or a falling hedging premium;
   equities up with yields down is policy easing (real-rate news) or a falling common premium;
   equities down with yields down is a rising hedging premium, the flight-to-safety case in which bonds hedge the sell-off, or bad growth news;
   equities down with yields up is a rising common premium, in which stocks and bonds sell off together, or a hawkish tightening.
   Report the quadrant with both candidates, and treat equity proxies that disagree in sign as an unresolved mixture.
   Credit and volatility play no part in this identification; record them as context, not confirmation.

4. Separate realized from expected: `premarket_return` is the previous-close-to-premarket leg and is already realized, and part of the overnight repricing can still arrive between the snapshot and the open, where it lands in the opening price rather than in the open-to-close target.
   Whether the move carries into the session is not settled by the quadrant: the overnight and regular-session legs are separate components with different clienteles, and the documented intraday momentum is narrower than an opening move continuing.
   It runs from the day's cumulative move up to the last half hour into that half hour, for the S&P 500 only when option dealers are net short gamma, which you cannot see, and the opening move adds nothing once the day's move is known. No cited study conditions carry on the mechanism.
   Make the call, but treat it as the weaker half of the thesis and give the opposite reading real weight in `antithesis`. A single risk-on or risk-off label is not sufficient.

5. Test breadth: require SPY, QQQ and IWM to agree in sign with no gross divergence in magnitude; compare raw premarket returns when no benchmark history is provided, and scale each move by that ETF's recent daily volatility when daily history is present.
   A size split (`IWM% - SPY%`) or style split (`SPY% - QQQ%`) that is large relative to the common move is factor rotation and leaves the broad-market contribution indeterminate.
   A move concentrated in one or two GICS sector ETFs is sector context, not a broad-market shock.

6. Map to the stock: when a relationship view is present, take `primary_exposure` and its `multi_etf_beta_126` and combine the expected open-to-close sign of that exposure with the sign of the beta;
   an ordinary positive equity beta gives the sign of the expected broad move, a low-beta or defensive exposure attenuates it.
   When no relationship view is present you are not told the stock's sector: find the sector ETF and broad-equity benchmark its recent daily history co-moves with most, and read only the sign and a rough size of that co-movement; do not report a fabricated beta.

7. Down-weight when the exposure is unreliable: if `beta_stability_checks_63_252` for the primary exposure disagree across the 63- and 252-session windows,
   or `residual_volatility` is large relative to the systematic push you estimated in step 6 (`sector_beta` times that sector ETF's premarket move), the beta channel does not support a direction; the relationship view gives no exact systematic-versus-idiosyncratic variance split.
   With daily history instead, make the same checks yourself: whether the stock's co-movement over roughly the last quarter agrees with the last year, and whether its own moves dwarf that co-movement.
   Treat a stated beta far from one as less decisive than it looks: when funding liquidity tightens, realized betas compress toward one because a funding shock pushes all prices down together, so a defensive name falls nearer the market and a high-beta name less than its number implies.
   None of your inputs measures funding liquidity, and a rising dollar with rising volatility does not identify it, so apply this only as a caution on magnitude, never as a reason to flip or discard the sign.

8. State the strongest contradiction: cross-asset disagreement, a premarket gap whose carry-versus-absorption call is genuinely uncertain, an unstable or stale exposure, rotation dressed as a market move,
   or equities and Treasuries selling off together during a volatility spike. That pattern is the rising-common-premium or tightening quadrant, and it can mean bonds have stopped hedging, as in the early-2025 US policy-credibility episode, when yields rose with volatility and the dollar weakened instead of rallying.
   Which stock-bond pattern counts as normal also depends on the regime: the stock-bond correlation runs higher when inflation and real rates are high.

## Abstain

Return `availability=no_data|not_entitled|provider_error` with `verdict=unavailable` only when the benchmark snapshots needed to classify the shock genuinely fail with that status;
the task `collection_status` names which. Return `availability=available, verdict=neutral` when benchmarks are present but describe size without a resolved direction:
an unresolved cross-asset mixture, disagreeing size or style baskets, a gap whose carry-versus-absorption call is too uncertain to support a direction, or an unstable market exposure.
The market-beta channel applies to every listed US equity, so `not_applicable` is not used for this domain.

## Output

Use the DomainReport contract with `component_type=market_beta`. The verdict is this component's contribution to the stock's absolute open-to-close return, not a whole-stock vote:
a decisive risk-off session is `bearish` for the market component of an ordinary positive-beta name even with no company news. Put the realized premarket move, the classified quadrant with its two candidate shocks, and the residual open-to-close expectation in separate sentences of `thesis`;
put the strongest counter-reading in `antithesis`; list unresolved inputs in `unknowns` and a concrete observable that would flip the call in `invalidation`. Cite the benchmark and relationship evidence IDs you used.
Do not emit probabilities, confidence, scores, rankings, or another domain's conclusion.

Read [foundations](references/foundations.md).
