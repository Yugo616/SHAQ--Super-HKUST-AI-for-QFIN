# Premarket Data Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make SHAQ distinguish observed values, provider limitations, and genuinely unavailable premarket/option/capital data without fabricating directional evidence.

**Architecture:** Extend the existing immutable evidence payloads additively at their collection boundaries, then make the human-readable replay consume the new quality state. Keep the current Yahoo provider and fail closed where no authorized source provides the required semantics.

**Tech Stack:** Python 3.11+, unittest, yfinance, immutable JSON evidence, shared macOS/Windows desktop code

**Spec:** `docs/superpowers/specs/2026-09-18-premarket-data-quality-design.md`

## Global Constraints

- Work only in the isolated repair checkout on branch `codex/fix-premarket-data-quality`.
- Do not modify the external read-only research workspace or frozen 2026-09-17 evidence.
- Preserve exactly the two existing research methods, all model settings, and fail-closed rules.
- Do not install, publish, run predictions, buy data, or use other application credentials.
- Do not add a production provider whose authorization, timestamp, or semantics are insufficient.

---

### Task 1: Premarket volume quality contract

**Files:**
- Modify: `src/shaq_daily_oracle/research_collection.py`
- Test: `tests/test_research_collection.py`

**Interfaces:**
- Consumes: existing intraday bar dictionaries accepted by `_premarket_state`
- Produces: additive `volume_status`, bar-count, `volume_ranking_eligible`, and `volume_note` fields

- [x] **Step 1: Write failing tests for explicit-zero, missing, mixed, and positive volume bars**

Use hand-built bar fixtures and assert literal status/count/eligibility values. The tests must fail because the existing implementation returns only a collapsed sum.

- [x] **Step 2: Run the focused tests and verify the expected failures**

Run: `.venv/bin/python -m unittest tests.test_research_collection -v`

- [x] **Step 3: Implement the minimal additive volume-quality classification**

Classify only finite non-negative fields as present. Keep price collection separate and use positive volume only as a ranking tie-breaker.

- [x] **Step 4: Run the focused tests and verify they pass**

Run: `.venv/bin/python -m unittest tests.test_research_collection -v`

### Task 2: Option chain coverage and quote quality

**Files:**
- Modify: `src/shaq_daily_oracle/data_providers.py`
- Test: `tests/test_yahoo_transport_retry.py`

**Interfaces:**
- Consumes: yfinance call/put DataFrames
- Produces: preserved `lastTradeDate`, response `captured_at`, per-expiry and aggregate `quality` dictionaries

- [x] **Step 1: Write a failing test with complete, partial, and invalid price pairs**

The fixture includes more source contracts than retained contracts, literal trade timestamps, zero/missing bid/ask, positive volume, and positive OI. Assert source counts, retained counts, valid price-pair counts, and timestamp semantics.

- [x] **Step 2: Run the focused test and verify it fails for missing quality fields**

Run: `.venv/bin/python -m unittest tests.test_yahoo_transport_retry.YahooTransportRetryTests.test_option_surface_separates_chain_coverage_from_quote_quality -v`

- [x] **Step 3: Implement a small quality summarizer and additive output fields**

Normalize pandas timestamps to ISO strings, count the source frames before truncation, count retained rows after truncation, and derive quote status without changing directional semantics.

- [x] **Step 5: Separate price-pair validity from quote freshness**

Label response capture time as transport capture time, retain last-trade timestamps only as trade timestamps, and keep quote freshness ineligible when exchange quote timestamps are unavailable.

Archive an untimed surface for diagnostics, but exclude it from derivatives task lineage until an adapter explicitly provides an eligible exchange quote timestamp.

- [x] **Step 6: Run all Yahoo provider tests**

Run: `.venv/bin/python -m unittest tests.test_yahoo_transport_retry -v`

### Task 3: Explicit provider limitations and readable replay

**Files:**
- Modify: `src/shaq_daily_oracle/research_collection.py`
- Modify: `src/shaq_daily_oracle/replay.py`
- Test: `tests/test_research_collection.py`
- Test: `tests/test_run_replay.py`

**Interfaces:**
- Consumes: new premarket volume quality fields
- Produces: explicit provider-status reasons and Chinese replay text that does not call provider-zero volume confirmed zero trading

- [x] **Step 1: Write failing assertions for provider reasons and replay wording**

Assert the capital and option-flow reason codes and verify an all-zero vendor response renders as unavailable participation evidence while positive volume retains the numeric display.

- [x] **Step 2: Run the focused tests and verify the expected failures**

Run: `.venv/bin/python -m unittest tests.test_research_collection tests.test_run_replay -v`

- [x] **Step 3: Implement the minimal status and rendering changes**

Read quality from the stock evidence; never infer direction or replace missing data with zero.

- [x] **Step 4: Run focused regression tests**

Run: `.venv/bin/python -m unittest tests.test_research_collection tests.test_run_replay -v`

### Task 4: Real-data evidence and final verification

**Files:**
- Create: `scripts/probe_free_market_data.py`
- Test: `tests/test_probe_free_market_data.py`

**Interfaces:**
- Consumes: direct no-key provider probe outputs and the immutable frozen sample
- Produces: exact raw HTTP responses and a hash manifest in a caller-selected directory outside the repository

- [x] **Step 1: Re-run no-key Yahoo and access-control probes without credentials**

Record HTTP status, exact response bytes, hashes, and the explicit ET sampling window outside Git. Never read credentials.

- [x] **Step 2: Run focused and full regression suites**

Run focused tests in the project virtual environment. Run the full suite from a path without spaces and with required process-inspection permissions so packaging tests exercise the intended environment.

- [x] **Step 3: Review the diff against the spec and mutation-check every new branch**

Confirm a wrong zero/missing branch, lost timestamp, wrong quote count, or misleading replay phrase would fail at least one test.

- [x] **Step 4: Commit the reviewed implementation**

Commit only the scoped source, tests, design, plan, and verification report. Do not push or publish.
