# Task 3 implementation report

Status: DONE. Base: `63bcb06`. Implementation and this report are committed together; the resulting commit ID is returned to the controller (no self-referential source hash embedded in source).

## Delivered

- Today is independently gated in `start_batch` before model-profile/credential/job creation and in `_today_evidence` before cache lookup/collection. Both use the existing NYSE calendar in New York time. Closed dates and pre-04:00 ET are blocked with the next session/date and an explicit reason; a normal NYSE Monday is allowed.
- Today no longer opts into `allow_replay`. The existing explicit historical collector route remains unchanged and historical frozen records are not mutated.
- Collection requires at least one valid current-day premarket stock observation before metadata/events/model work proceeds. Stale/empty/nonpositive minute prices cannot satisfy it. A silently empty result is `no_data: 未取得当天盘前数据`; an observed `DataProviderError` is `provider_error`, without inventing the provider's underlying cause. Partial coverage remains visible in frozen per-symbol premarket observations and existing stock states; no coverage percentage or voting threshold added.
- Cached Today evidence must have the exact same-day cutoff, same-day nonfuture capture, and valid current-day premarket observation status/timestamps. Invalid old locators fail closed, with no frozen-file rewrite or automatic destructive cleanup. Late weekday collection remains `late_research_only`; existing runner completion/deadline scoring rules remain unchanged.
- Actual workbench render disables Today based on backend availability and displays the reason. Existing editable automatic time/version selection, off-by-default schedule, per-version progress and refresh selection preservation are retained.
- Missing opening-minute trades display `开仓分钟缺失 · 未模拟成交`, without a misleading completed zero-share trade or undetermined $0 fees/profit. Day/result summaries use dashes for wholly unavailable entry data; existing partial fills and their actual costs stay visible and explicitly partial. Budget/volume non-fills and incomplete exits remain distinct. The balance plot now names date/trading-session and USD-balance axes, with no synthetic intraday path.
- Candidate detail summaries immediately score complete valid provisional prices, retain provisional/revised/final display state, and keep missing/invalid/unknown prices unscored. Flat prices remain wrong. Incomplete labels cannot display a confirmed-price badge. Original thesis/countercase/citations and the noncausal limitation remain visible.
- Version comparison shares the existing stronger known/homogeneous model identity logic with `compare_runs`: absent, unresolved or mixed audits do not establish same-model identity. No model request/runtime semantics changed.
- Method labels use `复制版本 / 保存修改 / 上传版本 / 下载团队版本`; changes are wording only.
- Sole authoritative project version is 0.7.0. Bilingual README and user/update guides describe three connection entries, CLI versus chat-app distinction, API-only setup, relay URL/key/model/context/group/protocol checks, bounded observed HTTP diagnostics, legacy full-install migration and managed native update behavior. Planned 0.7.0 platform pages are explicitly candidate/publication-gated. No false published timestamp, no self-referential source SHA, no rollback-copy promise.

## TDD evidence

All commands ran from `/tmp/shaq-lab-release-20260914` with `build/release-venv/bin/python`.

### RED before behavior changes

- `-m unittest discover -s tests -p 'test_today_guard.py' -v`: 6 tests, `FAILED (failures=8, errors=3)` after isolating collector dependencies. Reproduced reaching the credential path on weekend/holiday/HK-Monday-ET-Sunday/pre04, implicit collection on closed date, acceptance of all prior-day/empty minutes, absent observation manifest, unclassified provider exception and stale cache reuse.
- `-m unittest discover -s tests -p 'test_replay_summary.py' -v`: 6 tests, `FAILED (failures=2, errors=1)`: provisional result was None, absent model metadata compared equal, invalid final prices raised instead of staying unscored.
- `-m unittest discover -s tests -p 'test_account_view.py' -v`: 19 tests, `FAILED (failures=4)`: missing-entry zero-share/raw status/$0 display and absent axis labels.
- `-m unittest discover -s tests -p 'test_today_progress.py' -v`: new Today permission test failed because the actual button availability behavior did not exist.
- Added nonpositive-price regression after self-review: Today tests failed `ResearchCollectionError not raised`; positive-price validation made it pass.
- Added unavailable badge assertion after self-review: summary test failed `'provisional' != 'unavailable'`; valid prices are now required before assigning a scored/confirmed price phase.

### GREEN and regression acceptance

- Today backend/collector/cache tests: **10 passed**, including real frozen evidence reuse, stale prior-session rejection, no-data pre-LLM job termination, Monday partial coverage and late scope.
- Replay summary tests: **6 passed**.
- Account view tests: **19 passed**, including actual engine-produced partial fills.
- Today progress/render tests: **6 passed**, including executing the real workbench render and checking disabled button plus next-date/reason.
- Review view tests: **4 passed**; existing stock-selection/render and comparison behavior retained.
- Updater runtime/admission tests: **31 passed**.
- Final full command: `build/release-venv/bin/python -m unittest discover -s tests -v > /tmp/task3-full-suite.log 2>&1`: **Ran 503 tests in 13.265s — OK**, exit 0. This includes virtual accounts, minute-account integration, account views, model connections, native admission/generation, method aliases, frozen sizing, schedule and replay regressions.
- `git diff --check`: exit 0.
- `build/release-venv/bin/python -c 'from shaq_daily_oracle.app_paths import application_version; from shaq_daily_oracle.research_batch import _application_version; print(application_version()); print(_application_version())'`: **0.7.0 / 0.7.0**.

### Test-environment corrections

The first full run exposed test fixtures depending on the checkout still being 0.6.2. The failed-GUI-init test marked target0.6.2 but read source0.7.0 and entered the real recovery-window route, ending the process with exit134. Its paths were temporary, not the user's app/data. `UpdateRuntimeTests.setUp` now explicitly pins the simulated installation version to0.6.2 for both consumers; production updater/admission code was not relaxed. A new workbench render harness also needed its existing DOM/utility stubs completed; these setup errors were fixed before final full GREEN. Focused Zipline imports emitted upstream Python3.13 `co_lnotab` deprecation warnings; final full-suite output contains no failures/errors.

## Files changed

- `pyproject.toml`
- `src/shaq_daily_oracle/lab_service.py`, `research_collection.py`, `replay_summary.py`, `run_comparison.py`
- `src/shaq_daily_oracle/desktop/accounts.js`, `review.js`, `today_progress.js`, `workbench.js`
- `tests/test_today_guard.py`, `test_today_progress.py`, `test_replay_summary.py`, `test_account_view.py`, `test_update_runtime.py`
- `README.md`, `README.zh-CN.md`, `docs/user-guide.md`, `docs/software-updates.md`
- This report.

## Self-review and limits

- Reviewed complete code/doc diff. No changes to the two method bundles, account sizing/fees/fill calculations, historical trade replay/resize, formal canary workflow, connection runtime or accepted updater/generation admission implementation. No user installation, data, credential or GitHub push performed.
- The existing isolated postmortem contract is formal-canary-only; Lab has no associated saved provenance-bound postmortem writer/loader. Per controller clarification, no formal or arbitrary external material is imported. Lab displays price/direction/fee math and original thesis only and explicitly says prices cannot establish causes. No extra model call or invented attribution.
- Invalid legacy Today locators are rejected rather than rewritten. An explicit cache recovery/migration UI is outside this surgical task.
- `lab_service.py` and layered `workbench.js` are existing large files; changes remain small at existing boundaries. Model identity extraction prevents two comparison semantics from drifting.
- No claim of native packaged acceptance, published0.7.0 release or live user model/data connectivity. Root owns final reviewed-commit builds, platform acceptance, installed GUI verification and publication.

## Concrete real screenshot handoff

Root should capture the actual redacted target GUI, not a generated profit image: (1) Connection Settings with its three entry buttons and no key visible; (2) Start Runs showing current backend ET/session reason, disabled button if closed/pre04, per-version progress and editable/off automatic controls; (3) View Results using genuine saved records with USD/date axes and an actual unavailable-entry row, showing dashes and preserving any real partial fill; (4) provisional/revised detail showing math, original thesis and attribution limitation. Preserve source/version/time context in the acceptance artifact; do not fabricate a profitable run to fill the screenshots. Capture/attach only after real GUI health and exact target version checks.
