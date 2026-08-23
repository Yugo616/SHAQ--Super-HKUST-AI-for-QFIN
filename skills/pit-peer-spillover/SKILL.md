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

Return `unavailable` for present-day relationship tables backfilled into history, missing effective dates or unsupported company-name matching. Return `neutral` when correlation exists without a defensible mechanism.

## Output

Use the DomainReport contract. Name the relationship, sign mechanism, effective date and supporting evidence IDs. Do not call two reports independent when they descend from the same event.

Read [foundations](references/foundations.md).
