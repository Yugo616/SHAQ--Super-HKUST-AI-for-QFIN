# Premarket Data Quality Design

## Problem

The free Yahoo research path currently collapses distinct facts into misleading zeros:

- Premarket price bars can be present while every volume field is explicitly zero. The application records this as an observed volume of `0`, although the source is not sufficient to prove that no shares traded.
- An option chain can contain many contracts, volume, and open interest while most retained rows lack a valid bid/ask price pair. The application reports only `collected` and omits trade/capture timestamps, so chain coverage, price-pair validity, and quote freshness cannot be distinguished.
- The free path has no event-level aggressor classification plus order-book depth. That domain must remain unavailable instead of being synthesized from price or quote proxies.

An external read-only VRT sample from 2026-09-17 demonstrates all three cases. Raw samples and internal verification logs remain outside the repository and release payload.

## Design

### Premarket volume

`_premarket_state` will keep price-bar collection status separate from volume quality. It will report:

- `volume_status`: `observed_positive`, `provider_reported_zero`, `partially_missing`, `missing`, or `no_price_bars`;
- counts of eligible bars, positive-volume bars, zero-volume bars, and missing-volume bars;
- `observed_volume` only when at least one volume field is present, while explicitly warning that all-zero vendor fields do not prove zero trading;
- `volume_ranking_eligible`, true only when every eligible price bar has a volume value and at least one is positive.

Candidate ranking will use volume only when `volume_ranking_eligible` is true. Price returns remain usable when price bars are valid.

### Option surface

The Yahoo option adapter will preserve `lastTradeDate` and add one capture timestamp for the response. For each expiry and for the aggregate response it will publish separate coverage and quality counts:

- source and retained contract counts;
- contracts with numerically valid two-sided prices;
- contracts with positive reported volume;
- contracts with positive open interest;
- price-pair coverage status (`complete`, `partial`, or `unavailable`);
- quote freshness as `unverifiable` because the provider supplies no exchange quote timestamp.

`lastTradeDate` is labeled as a last-trade timestamp, never as a quote timestamp. The response capture time is labeled as transport capture time, not exchange quote time. Directional-flow and freshness eligibility remain false.

The raw surface is still archived for diagnostics, but it is not registered as derivatives task evidence unless an adapter explicitly supplies an exchange quote timestamp and marks quote freshness eligible. This prevents a downstream task from substituting response capture time or last-trade time for quote time.

### Provider availability

`raw/provider-status.json` will use explicit machine-readable reasons:

- capital: `requires_authorized_aggressor_and_depth_feed`;
- option trade flow: `surface_without_aggressor_or_open_close_semantics`.

The application will not infer buy/sell pressure from price changes, volume, bid/ask, put/call ratios, or open interest. Existing authorized Futu deep capture remains an optional separate path; no credentials are added or reused.

### Human-readable replay

Price/volume replay text will show an all-zero vendor response as unavailable participation evidence, not “0 shares traded.” Existing positive volume remains displayed normally.

## Alternative-source decision

No-key probes and official documentation support this fail-closed result:

- Alpaca Basic requires `APCA-API-KEY-ID` and `APCA-API-SECRET-KEY`. Its free real-time equities feed is IEX-only; delayed consolidated history cannot cover the final 15 minutes before the 08:50 ET cutoff.
- Massive Stocks Basic requires a free API key and can supply end-of-day historical minute aggregates, including premarket bars, but not the same-session 08:50 ET input.
- Twelve Data requires an API key and limits current U.S. extended-hours data to paid tiers; free historical extended-hours records are not same-session.
- Alpha Vantage documents extended-hours OHLCV, but classifies the intraday endpoint as premium.
- Cboe's delayed quote page forbids automated extraction.
- Nasdaq's public page endpoint returned a fuller VRT chain in a diagnostic probe, but it does not provide a contractual automated API, per-contract quote timestamps, or aggressor/open-close semantics.
- No free source identified provides complete consolidated volume through the cutoff, exchange-timestamped option quotes, and event-level aggressor direction plus order-book depth for the required US premarket scope.

Therefore no new production provider is added in this repair. Required external access is reported, not silently substituted.

## Compatibility and safety

- Shared Python collection and replay code is used by macOS and Windows.
- Existing two research methods, model profiles, prediction rules, and immutable evidence remain unchanged.
- No prediction, installation, release, purchase, or credentialed request is part of this work.
- Existing evidence schemas are extended additively.

## Acceptance criteria

1. A price series with all explicit zero volume fields is labeled `provider_reported_zero`, not missing and not positive.
2. A price series with absent volume fields is labeled `missing` and has `observed_volume = null`.
3. Candidate volume tie-breaking ignores non-positive/unusable volume observations.
4. Option output preserves `lastTradeDate` and independently reports full-source coverage, retained rows, price-pair validity, and unverifiable quote freshness.
5. An option surface with contracts but no valid bid/ask pair remains a collected chain with price-pair status `unavailable`, not “zero contracts.”
6. Capital and option-flow limitations remain fail-closed and explicit.
7. Replay text never describes provider-reported all-zero volume as confirmed zero trading.
8. Focused tests and regression tests cover the behavior; a reproducible no-key probe stores raw responses only outside the repository.
