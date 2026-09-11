# Task 1 report: local account continuity, risk sizing and settlement schedule

## Outcome

Implemented a reviewed activation/reconciliation boundary for the experimental account rules without touching the user runtime. Historical settlements are content-addressed, read-only sources for a simulation-cumulative bridge; their saved fills and quantities are not re-executed under the new policy. Same verified method identity plus the same model identity shares one balance stream even when the display/version alias changes. Forward performance remains separately labelled.

New forward predictions can freeze point-in-time unadjusted `close/open-1` volatility inputs with sample standard deviation (`ddof=1`). Risk sizing uses opening equity × 0.002 / sigma, a 10% per-symbol cap and 30% gross cap. Missing, nonpositive, nonfinite, insufficient, or future-dated inputs produce an explicit no-fill reason. Legacy predictions without risk fields retain the fixed-budget path.

Post-close collection now has a pure local due planner and exact-date filtering at both daily-label and minute-observation fetch boundaries. It uses close +5/+15/+30/+60 minute finite attempts, persists attempts, skips reviewed dates, supports next-session independent confirmation and one app-open fallback per local day, and relies on the exchange calendar for weekends, early closes and DST. The research worker waits for an in-flight refresh before exiting. The UI retains active-job 10-second polling, replaces the unconditional 15-minute refresh with a local 60-second due check, describes the experimental rule accurately, and uses the requested action labels.

## Controller entrypoints

- Callable preview (no writes): `reconcile_activation(AccountStore(root), rows, activated_at=..., apply=False)`
- Callable activation: same call with `apply=True`
- CLI preview: `python -m shaq_daily_oracle.account_activation --account-root ROOT --rows-json ROWS.json --activated-at TIMESTAMP`
- CLI activation: add `--apply` only after controller review.

Preview returns the matching source settlement hashes, source scopes, saved net P/L and projected seed per verified method/model identity. Activation writes an explicit activation record containing the predecessor activation hash and then reconciles. A brand-new profile still receives the existing local fixed-budget default; opening the app does not read credentials for account status. The experimental continuity policy remains explicit-controller-only.

## Changed files

- `src/shaq_daily_oracle/account_activation.py`
- `src/shaq_daily_oracle/virtual_accounts.py`
- `src/shaq_daily_oracle/minute_execution.py`
- `src/shaq_daily_oracle/minute_settlements.py`
- `src/shaq_daily_oracle/research_labels.py`
- `src/shaq_daily_oracle/lab_service.py`
- `src/shaq_daily_oracle/research_schedule.py`
- `src/shaq_daily_oracle/research_batch.py`
- `src/shaq_daily_oracle/lab_smoke.py`
- `src/shaq_daily_oracle/desktop/accounts.js`
- `src/shaq_daily_oracle/desktop/app.js`
- `src/shaq_daily_oracle/desktop/workbench.js`
- `tests/test_virtual_accounts.py`
- `tests/test_minute_accounts_integration.py`

## TDD and verification evidence

RED: three initial account tests failed with missing `experimental_risk_rules`, `reconcile_activation`, and continuity behavior. The volatility-freeze test then failed with missing `freeze_risk_sizing`. Each was observed failing before implementation.

GREEN targeted account/scheduler checks passed. The requested full discovery run executed 377 tests and initially reported 8 failures. Seven were caused by overly broad saved-settlement reuse; the eighth pair was the same smoke contract failure. The implementation was narrowed so only a new continuity policy reads predecessor historical settlements, while same-policy price revisions continue to derive a new projection. No old-policy loop is replayed after continuity activation. The smoke contract was updated to omit inactive optional risk fields for legacy rules.

Fresh post-fix regression command:

`PYTHONPATH=src:tests .../venv-nolzo/bin/python -m unittest tests.test_lab_smoke... tests.test_virtual_accounts tests.test_minute_accounts_integration tests.test_result_refresh tests.test_account_view tests.test_workbench tests.test_minute_execution tests.test_research_labels tests.test_today_progress tests.test_result_refresh_ui`

Result: `Ran 117 tests in 4.940s — OK`. `compileall` and `git diff --check` also passed.

The full discovery test contains packaging/installer simulation tests; it emitted their fixture output despite the task prohibition on doing a product build/install. No product artifact was built, installed, published, pushed, or applied to user data by this task. I did not repeat that discovery command after fixing the failures; the exact previously failing tests plus adjacent account, scheduler, UI, label and engine suites are included in the 117-test post-fix run.

## Remaining concerns for controller review

- The controller must supply the real read-only dashboard rows and verify projected Sep 9 → Sep 11 seed values before `--apply`.
- `run_variant` calls `freeze_risk_sizing` before writing a genuinely new result only when the frozen evidence timestamp is at or after the risk-policy activation. It reads only that evidence bundle's archived stock bars; reused/old result documents return before this branch and remain byte-identical.
- A predecessor price revision can change a pending successor's projected seed until that successor first settles. Once a successor settlement exists, controller review should require an explicit revision chain rather than silently changing its saved quantities.
- No user runtime migration, activation, build, installation, dependency change, network/model call, or push was performed.
