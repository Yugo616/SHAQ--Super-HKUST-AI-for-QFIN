---
name: daily-oracle
description: Orchestrate a reference-grounded US premarket stock forecast from six blinded evidence domains, freeze zero to three official open-to-close directions, and prepare fail-closed Futu paper-canary artifacts. Use for Daily Oracle, V6 premarket analysis, multi-agent stock direction, forecast audit, or paper-canary requests.
---

# Daily Oracle

Produce a small, auditable forecast rather than a theatrical panel of Agents.

## Entry point

Run `daily-oracle run --mode paper`. Treat it as the only operating interface. It performs governed collection, six-domain analysis, freeze, Futu `SIMULATE` canary and audit. Resume the same command after interruption; never call a domain Skill or stage script manually in production.

## Workflow

1. Set `as_of_et`, cutoff and target: official unadjusted US regular-session open to close.
2. Freeze source files. Require first-publication time, capture time, URI and actual SHA-256.
3. Build the lineage graph. Shared raw files, upstream events and transforms form one root.
4. Give each domain only its own evidence. Hide ranks, core direction, labels and other reports.
5. Invoke all six domain skills. Preserve `neutral` and `unavailable`; never force coverage.
6. Validate every report against `schemas/domain-report.schema.json` and verified lineage roots.
7. Invoke `thesis-adversary` once. It may veto but cannot vote, add facts or multiply evidence.
8. Require two unconflicted roots: one causal/context domain and one independent market-absorption domain. Reject any independent opposing root.
9. Freeze zero to three predictions. Do not count Agents, words or copied lineage roots.
10. If created after cutoff, mark the run `shadow`. Never promote it later.
11. In eligible canary mode, refresh the Futu `SIMULATE` account, classify non-V6 positions as external, and create one-share intents. A bearish intent requires confirmed borrowability.
12. Keep official open-to-close labels and actual fill-to-fill trading results in separate fields.

## Mandatory gates

- Reject missing files, SHA mismatch, lineage cycles, orphan transforms and post-cutoff material.
- Reject reports containing self-declared probability, confidence, score or unseen facts.
- Keep `p_committee_hit` and `p_net_profit` null until prospective statistical gates pass.
- Never enable real trading. Never alter or attribute any external position.
- On stale data, broker ambiguity, duplicate intent or failed reconciliation, add no risk.

## Output

Return the frozen evidence manifest, six domain reports, adversary report, zero-to-three predictions, and either canary or shadow intents. State the strongest countercase beside every published direction.

Read [foundations](references/foundations.md) for the selected agentic design and statistical boundaries.
