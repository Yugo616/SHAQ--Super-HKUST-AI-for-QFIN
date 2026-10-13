# SHAQ Desktop Reliability Implementation Plan

> **For agentic workers:** Use test-driven development for behavioral fixes and verification-before-completion before delivery. Independent audits and file-disjoint work may use dispatching-parallel-agents. Continue in the existing local checkout as requested by the user.

**Goal:** Deliver necessary-evidence coverage, efficient isolated recovery, stable flat desktop interaction and verified cross-platform updates.

**Architecture:** Preserve the existing research and native desktop architecture. Add a lifecycle-owned activity notification boundary, persistent DOM updates, and independent release discovery. Carry forward saved fixes and publish only the selected source changes from a clean public baseline.

**Tech Stack:** Python 3.13, pywebview, vanilla JavaScript/CSS, unittest and Node behavior checks, existing native packaging and Velopack.

**Spec:** `docs/superpowers/specs/2026-10-13-desktop-reliability-design.md`

## Global Constraints

- Continue in the existing local checkout; no new worktree.
- No model or paid-data calls for implementation verification; preserve successful saved analysis.
- Preserve two methods, model selection, account rules, frozen data and published predictions.
- Actual market-session open is the eligibility deadline, with the existing exact-minute simulation rules.
- No new desktop framework or unnecessary data-source dependency.
- Avoid hardcoded user paths, accounts, target stocks and platform-specific release assumptions.

## Review Focus

- External scheduler activity and reconnect after a missed notification must appear without refreshing history continuously.
- Update checking must not block research progress or enable automatic installation.
- Early stock failure must stop further fresh work on that stock while retaining cache and healthy-stock results.
- Calendar-only and cross-domain context must not be promoted to fabricated directional company events.
- Closing fixture windows must drain observers before Windows temporary-file cleanup.

### Task 1: Evidence and candidate execution

**Files:** `research_collection.py`, `research_batch.py`, focused collection/isolation tests; timing and account tests as verification.

**Interfaces:** Frozen records retain source identifiers and timestamps. Domain tasks retain canonical per-format envelopes. Excluded candidates remain explicit and recoverable.

- [x] Add a failing end-to-end fixture for a candidate with calendar evidence and no SEC document; check unrelated broad context remains non-event.
- [x] Correct only the new calendar record's domain and verify scoped calendar semantics.
- [x] Add an early-domain failure fixture proving later fresh calls omit the excluded stock and healthy output is retained.
- [x] Preserve successful partial reports for original-batch recovery; verify shared failures still stop publication.
- [x] Verify pre-open, exactly-open, late, holiday and daylight-saving eligibility reaches daily account selection correctly.

### Task 2: Desktop notifications and persistent progress

**Files:** new `desktop_activity.py`, `desktop.py`, `desktop/app.js`, `desktop/today_progress.js`, `desktop/workbench.js`, relevant activity/lifecycle tests.

**Interfaces:** Notification payload is the existing `get_lab_activity` result. The native owner starts after readiness and stops before GUI cleanup. The frontend consumes changed snapshots idempotently and keeps a reconnect path.

- [x] Add lifecycle tests for prompt changed-state delivery, unchanged suppression, observer failure recovery and shutdown drain.
- [x] Implement the small configurable native observer and attach it to the window lifecycle.
- [x] Apply changed snapshots through one frontend function; remove the ten-second UI polling interval.
- [x] Keep progress/report DOM nodes where their identity is unchanged; update clocks in place once per second and stop them at terminal state.
- [x] Verify external job discovery, selected report/scroll/focus preservation and no fabricated progress.

### Task 3: Automatic discovery and compact update control

**Files:** `software_updates.py`, `desktop/software_updates.js`, `desktop/index.html`, updater behavior tests.

**Interfaces:** Existing platform-aware release states drive the header indicator. `automatic_enabled` continues to govern installation rather than discovery.

- [x] Add failing tests that release discovery works with automatic installation disabled and does not download/apply.
- [x] Add button-state behavior tests for current, local-newer, no-release, available, downloading, ready and errors.
- [x] Check on startup and at the configured interval; deliver changes without blocking activity notifications.
- [x] Show only the applicable compact icon and preserve manual settings/recovery, work exclusion and draft handling.

### Task 4: Three-page flat desktop

**Files:** `desktop/index.html`, new `desktop/flat.css`, `desktop/workbench.js`, `desktop/review.js`, native GUI fixture assertions where interaction changes; shared SVG and platform icon assets.

**Interfaces:** Preserve bridge operations, element identities relied on by tests, account/report selection and accessible control labels.

- [x] Replace the table-heavy run setup with compact method rows and a clear single primary action.
- [x] Restyle navigation, editor, result views and dialogs with a neutral flat design and restrained sizing.
- [x] Keep data/automatic settings secondary and ensure long Chinese labels/model names wrap.
- [x] Run actual native GUI fixture checks and inspect all three pages at the supported minimum window size.

### Task 5: Verification and selective public integration

**Files:** precise saved repair files, `tests/test_result_refresh.py`, version metadata, release documentation.

- [x] Complete source validation after public ancestry integration; focused tests, all 1222 offline tests (7 conditional skips) and independent whole-change review passed.
- [x] Preserve the validated Windows fixture bounded-wait and worker-drain correction.
- [x] Prepare public ancestry from the published runtime baseline in the same checkout, retaining a recoverable local snapshot.
- [x] Update coherent version metadata above 0.7.11 and validate payload/privacy/version identities.
- [ ] Dispatch a single final source to the three native platforms; inspect job-level evidence, retain successful artifacts and retry only failed affected jobs.
- [ ] Publish matching platform installers and update feeds only after native acceptance, then verify local installation/data preservation and remote release contents.

## Execution record

Initial state: twelve saved citation/isolation repair files, local installed 0.7.11, public runtime baseline 0.7.10. No implementation, model calls, builds or publication performed while preparing this plan.

Implementation verification: the native Mac layout fixture passes all sixteen normal/narrow surface checks, persistent progress-node identity, independent manual/scheduled selection, formal-operator updates and disclosure retention. The separate native GUI smoke passes connection, replay, comparison, updater and saved-editor interactions. Independent review found and verified fixes for shared-failure checkpoint reads, accumulated recovery attempts, renderer-close blocking, late full-response state races and checkbox identity collisions. Live model calls and personal provider/account behavior were not exercised.

The user selected the flat Q containing a market signal. Version metadata is 0.7.12 so it is newer than the locally installed 0.7.11. Public ancestry and three-platform delivery remain separate verification steps.

Final user release naming: 0.9.0 Preview supersedes the earlier 0.7.12 candidate and the briefly proposed 1.0.0 name. Resolve native acceptance findings before freezing and delivering the final three-platform source.

Native acceptance found two additional boundaries. Windows icon customization changes the official updater's resource section; the audit now verifies the locked vendor's non-resource code sections while retaining private-path checks over every byte. A real Mac RPC-return/close race left a non-daemon thread waiting on a native JavaScript semaphore after the window closed. The desktop, installed-update acceptance and update-recovery windows now use a window-owned cancellation adapter for rendering waits. API work stays on its original thread and completes before its response is discarded after closure. Deterministic concurrency tests cover cancellation, original results and exceptions, and preservation of an admitted file write.

Final source checks: 1222 offline tests passed with seven conditional platform skips; the native GUI interaction fixture and all sixteen normal/narrow layout surfaces passed. Independent native verification repeated the original close barrier for all three window entrypoints plus normal desktop and update controls: all five real processes exited successfully within the unchanged deadline. Public-source validation passed with all eight Skills. Three-platform package acceptance and publication remain the next delivery gates.
