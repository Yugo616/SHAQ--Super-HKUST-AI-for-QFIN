# Task 1 report: end-to-end research presentation

## Commit

- `95c4a06 Show live research execution progress`

## Scope delivered

- Added an append-only JSONL research observer keyed by batch, variant, symbol(s), domain, call identity, attempt, timestamp, status, and measured elapsed seconds.
- Instrumented preparation, screening, domain-call start/return, cache hit, no-data, validated report, validation failure, adversary, decision, and terminal failure boundaries.
- Kept observer writes and reads non-blocking. Raw invalid model output is never recorded or rendered; only contract-validated reports enter the conclusion view.
- Exposed incremental events through the existing `LabService` job polling and completed batch-detail APIs.
- Added current-day and history research views with version/candidate selectors, task counts, elapsed time (no invented percentage), support/counterevidence/unknowns/invalidation, source/time/unit detail, original validated-report foldouts, safe escaping, and legacy-history disclosure.
- Preserved native duplicate-run controls and replay selection; expanded sections and selected candidate/version remain in in-memory UI state across the normal refresh wrapper.
- Extended the deterministic `lab_smoke` fixture to emit the same presentation events without networking, live model calls, installed-runtime changes, or permanent user history.
- Confirmed forward virtual accounts inherit the prior close as the next session opening cash for the same method+model identity; the UI now labels period opening and closing balances. Historical scope remains isolated.
- Bumped source-tree version only to `0.6.2.dev1`; no publishing or installed-app mutation was performed.

## TDD evidence

RED (before implementation):

```text
PYTHONPATH=src:tests .../venv-nolzo/bin/python -m unittest tests.test_research_progress tests.test_today_progress
TypeError: ui.researchHtml is not a function
FAILED (errors=2)

PYTHONPATH=src:tests .../venv-nolzo/bin/python -m unittest tests.test_research_batch.ResearchBatchTests.test_progress_observer_is_isolated_and_does_not_change_predictions
TypeError: ResearchBatchRunner.run() got an unexpected keyword argument 'observer'
FAILED (errors=1)
```

GREEN targeted:

```text
PYTHONPATH=src:tests .../venv-nolzo/bin/python -m unittest tests.test_research_progress tests.test_today_progress tests.test_research_batch tests.test_virtual_accounts tests.test_lab_smoke
Ran 34 tests in 3.655s
OK
```

Full regression:

```text
PYTHONPATH=src:tests .../venv-nolzo/bin/python -m unittest discover -s tests
Ran 366 tests in 9.775s
OK
```

Eight-Skill/release validation command:

```text
PYTHONPATH=src:tests .../venv-nolzo/bin/python scripts/validate_release.py
ValueError: local or legacy path in public package: .../local-results-plan.md
```

This check reaches the repository-wide privacy scan after validating exactly eight Skills. Its only failure is a pre-existing untracked planning file outside task scope; no product source failure was reported.

Deterministic GUI/smoke reproducer:

```text
smoke_dir=$(mktemp -d /private/tmp/shaq-progress-smoke.XXXXXX)
PYTHONPATH=src:tests .../venv-nolzo/bin/python -m shaq_daily_oracle.lab_smoke --package-root . --output "$smoke_dir"
```

Observed: `status=passed`, `fixture_kind=deterministic-model-fixture`, 30 events, with preparation, screening, domain call start/return, validated report, adversary, and decision stages.

## Identity and safety checks

- The observer is not included in prompts, schemas, cache keys, model profile identity, method identity, portfolio rules, or prediction inputs.
- An observer exception is swallowed at the sidecar boundary; a dedicated regression confirms predictions are identical with and without it.
- Multi-symbol calls store the exact symbol list on the single real call event; cache reuse is marked as cache reuse and is not represented as an independent model call.
- No live model/provider call, broker action, installation, or app restart occurred.

## Concerns for controller review

- The controller should remove or exclude the pre-existing `local-results-plan.md` from the release candidate before treating `scripts/validate_release.py` as green.
- Native rendering/build review remains controller-owned. Use the deterministic smoke output for the new timeline; today's real completed batch is correctly treated as legacy and must not be backfilled with fabricated timestamps.

## Reviewer round 1 fixes

- Made both the in-process lock and JSONL file lock zero-wait; contention or filesystem failure now drops only the observer event.
- Replaced ambiguous call start/return events with `call_requested`, `model_started`, `model_returned`, and `cache_hit`. Cache hits never claim that a model started.
- Bound the exact content-addressed cache key to every group/task and reused the explicit attempt from request through the lifecycle. Whole-variant reuse emits reuse and validated-report events without fabricated calls.
- Emitted deterministic no-data reports even in mixed model/no-data candidate groups.
- Moved live selector, inner-section, and outer-section state into `wb.researchSelections`, which survives service-state replacement during polling.
- Reworked counts around distinct call/report/stage task identities, terminal states and in-flight requests; wall elapsed is derived from event timestamps rather than summing overlapping stages. Timeline now includes all lifecycle, validation, adversary, decision, reuse and failure rows.
- Added frozen observed evidence values alongside source, capture time, and unit in the primary report layout; no derived financial signal is calculated.
- Removed the Python invalid-escape warning and added a zero-wait locked-sidecar regression.

Round 1 verification:

```text
PYTHONPATH=src:tests .../venv-nolzo/bin/python -m unittest discover -s tests
Ran 367 tests in 9.225s
OK
```

## Reviewer round 2 fixes

- Validated and validation-failure events now carry the exact originating group call ID and the same explicit attempt as request/start/return.
- Domain-call failures carry the affected symbol list, call ID and attempt, so a selected-stock view shows the failure and closes the in-flight task.
- Live inner foldouts persist immediately on toggle, independently of selector changes and service polling; the outer research foldout remains persisted as well.
- `model_started` is emitted only after the rate-limit wait, immediately before invoking the caller. The preceding requested state honestly represents queued/waiting work.
- Report rows use report identity before call identity in task counts, avoiding phantom model attempts. Active wall time advances using the current clock until a terminal event arrives.

Targeted verification: 22 tests passed. Full verification:

```text
Ran 367 tests in 9.658s
OK
```

## Reviewer round 3 fix

- Added stable `data-research-section` identities for the execution timeline and each symbol/domain original-report foldout. Both now participate in the same persisted open-state store as domain sections and remain open after polling redraws.
- Targeted JavaScript/navigation verification: 8 tests passed.
