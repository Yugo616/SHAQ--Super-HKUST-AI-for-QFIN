---
name: primary-event-reasoner
description: Evaluate a company event from point-in-time SEC, issuer IR and official materials by separating new facts, expectation gap, first-publication time and premarket price absorption. Use for earnings, guidance, filings, contracts, regulatory events and other company-event analysis in Daily Oracle.
---

# Primary Event Reasoner

## Method

1. Establish the first public timestamp from SEC acceptance, issuer IR or another primary source.
2. Extract objective new facts with exact source spans. Distinguish reported results, forward guidance and management interpretation.
3. Compare with an expectation that itself existed before release; if absent, mark surprise unknown.
4. Identify the transmission mechanism to cash flow, margin, financing, dilution, regulation or execution risk.
5. Measure absorption using only cutoff-safe premarket path and participation.
6. Decide whether residual information can affect open-to-close, not whether the headline sounds positive.
7. State the strongest counter-reading and the observable condition that invalidates the thesis.

## Abstain

Reject second-hand summaries when primary material exists. Return `unavailable` when first-publication time is not verifiable or when content was overwritten. Return `neutral` when facts are real but direction depends on an unknown expectation.

## Output

Use the DomainReport contract and cite only supplied evidence IDs. Do not add remembered facts, later commentary or post-open prices.

Read [foundations](references/foundations.md).
