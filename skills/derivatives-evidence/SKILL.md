---
name: derivatives-evidence
description: Interpret options through implied move, distribution, skew, term structure and only semantically reliable directional flow; never map puts, calls or open interest mechanically to stock direction. Use for the derivatives domain of Daily Oracle, unusual-options activity, IV, skew, put-call or implied-move analysis.
---

# Derivatives Evidence

## Method

1. Validate underlying price, chain timestamp, expiry, strike, bid/ask, volume and open interest.
2. Build the implied move and compare skew and term structure across matched maturities.
3. Prefer a cleaned implied distribution over a put/call headline.
4. Form directional flow evidence only when buyer/seller initiation and opening/closing semantics are reliable.
5. Use next-session open-interest change only to confirm that a prior position likely remained open; it does not identify holder intent by itself.
6. Check put-call parity or matched call-put IV deviations only after dividends, rates, borrow and American exercise are addressed.
7. State alternative structures: hedge, spread, covered call, volatility trade or stale quote.

## Abstain

Return `neutral` when options only imply volatility. Return `unavailable` for incomplete chains, crossed/stale quotes or missing directional semantics. Max pain never enters direction.

## Output

Use the DomainReport contract. Separate expected range from direction and name every semantic assumption.

Read [foundations](references/foundations.md).
