---
name: pit-peer-spillover
description: Analyze point-in-time customer, supplier, competitor, complement and multi-ETF relationships to determine whether a premarket event should spill into a stock's regular-session direction. Use for the relationship or industry domain of Daily Oracle and peer-event analysis.
---

# PIT Peer Spillover

## Method

1. Use only relationships known by cutoff; record effective dates and source documents.
2. Name the economic edge: customer demand, supplier input, competition/substitution, complementarity, or shared factor exposure.
3. Determine the sign implied by that edge. Competitor news need not have the same sign as supplier news.
4. Use multi-ETF residual exposure and historical residual co-movement only as confirmation.
5. Check event-company premarket reaction and whether the information is already absorbed.
6. State the strongest alternative edge or reason the relationship is stale.

## Abstain

Return `no_data + unavailable` when the only relationship evidence is a present-day table backfilled into history, lacks effective dates or relies on unsupported company-name matching. Industry beta can remain `available + neutral` without a named company relationship; never invent one from correlation.

## Output

Use the DomainReport contract with `component_type=industry_spillover`. The verdict is the industry/relationship contribution to absolute return. Do not call two reports independent when they descend from the same event.

Read [foundations](references/foundations.md).
