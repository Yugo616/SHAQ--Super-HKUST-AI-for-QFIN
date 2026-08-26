# SHAQ Daily Oracle

SHAQ Daily Oracle is a reference-grounded workflow for forecasting the absolute direction of US stocks from the official regular-session open to close. Six professional domain skills analyze frozen evidence, a non-voting adversary checks the result, and deterministic code enforces time, provenance, independence and paper-trading safety.

## Architecture

```text
cutoff-safe evidence
        |
        +-- market common shock
        +-- company relationships
        +-- primary company event
        +-- capital and order flow
        +-- options and derivatives
        +-- price-volume structure
        |
        v
evidence-lineage integrator --> non-voting adversary --> 0-3 forecasts
                                                        |
                                                        v
                                              Futu SIMULATE canary
```

The system does not count Agents or reports as votes. Every raw observation keeps its own provenance root. A derived item carries the set of all ancestor roots without merging them; copies of the same raw file or event still count once.

## Skills

| Skill | Responsibility |
|---|---|
| `daily-oracle` | Freeze and orchestrate the complete workflow |
| `market-common-shock` | Separate cash-flow, rate and risk-premium shocks |
| `pit-peer-spillover` | Analyze point-in-time customer, supplier, competitor and complement links |
| `primary-event-reasoner` | Separate new facts, prior expectations, publication time and price absorption |
| `capital-order-flow` | Use liquidity-adjusted order-flow imbalance when the required semantics exist |
| `derivatives-evidence` | Interpret implied distributions and semantically reliable option flow |
| `price-volume-structure` | Classify nonlinear price paths, participation and liquidity states |
| `thesis-adversary` | Detect duplicated evidence, unsupported facts and horizon mismatch without voting |

Each skill contains a concise `SKILL.md` and a one-level `references/foundations.md` file. The reference file states the research mechanism used by the skill; it does not copy an external paper's performance claim.

## Install and run

No Codex installation is required to review the architecture. Start with `skills/daily-oracle/SKILL.md`, then inspect the six domain skills and their foundation files. The `src/` package contains the deterministic evidence, integration, evaluation and broker-safety controls.

Install the standalone Python safety core from the repository:

```bash
git clone https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN.git shaq-daily-oracle
cd shaq-daily-oracle
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e ".[test]"
python3 scripts/validate_release.py
python3 -m unittest discover -s tests -v
```

Install the optional Futu adapter only on a machine that already runs Futu OpenD:

```bash
python3 -m pip install -e ".[futu]"
```

Set machine-specific inputs without editing the repository:

```bash
export DAILY_ORACLE_UNIVERSE=/path/to/effective-universe.csv
export DAILY_ORACLE_SEC_USER_AGENT="Research Team contact@example.edu"
```

Then use the single operating command:

```bash
daily-oracle run --mode paper
```

It starts preflight at 08:35 ET, freezes evidence at 08:50, freezes the forecast by 09:00, opens eligible one-share `SIMULATE` positions during 09:30–09:35, and exits during 15:55–15:58. Re-running the command resumes immutable stage files and broker idempotency keys.

The command invokes the six skills internally. Reviewers consume `professor_report.html` and `agent_trace.html`; they do not need to know Skill names or write prompts. Only `daily-oracle` allows implicit Skill invocation. The domain skills and adversary are internal components.

## Decision contract

1. Freeze evidence available by the configured premarket cutoff.
2. Verify source URI, first-publication time, capture time and actual SHA-256.
3. Build a W3C PROV-style ancestry graph across raw files, upstream events and transformations.
4. Route the raw inputs required by each domain while excluding ranks, labels and other Agent reports.
5. Run all six domains and distinguish usable-neutral, not-applicable, no-data, entitlement and provider failures.
6. Require two applicable aligned domains, two independent roots, one market/industry root and one stock-specific root, with no independent opposing root.
7. Run the adversary once; it may veto an integrity failure but cannot add a vote or new evidence.
8. Freeze zero to three predictions. A run created after cutoff is permanently marked `shadow`.

The common domain output is:

```text
domain
as_of_et
horizon
availability: available | no_data | not_entitled | provider_error
verdict: bullish | bearish | neutral | not_applicable | unavailable
component_type
thesis
antithesis
unknowns
invalidation
evidence_ids
lineage_root_ids
```

## Evaluation and paper trading

- The scientific label is the official unadjusted US regular-session open-to-close return.
- The execution ledger separately records actual arrival prices, fills, fees and implementation shortfall.
- Probability output is generated only from frozen prospective records after the configured proper-score and sample gates are satisfied.
- Futu execution is locked to `SIMULATE`; external positions are excluded and real trading is not supported.
- Broker ambiguity, stale inputs, duplicate intents or failed reconciliation add no new risk.

## Repository scope

The repository contains code, schemas, tests, Skill instructions and research references. It contains no broker credentials, runtime snapshots, redistributed market data, internal research logs or real-trading switch. No software license is granted in this review version.
