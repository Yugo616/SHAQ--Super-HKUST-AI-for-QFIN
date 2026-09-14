# Task 4e packaging correction report

## Scope and diagnosis

Base: `24ff16b`, isolated release checkout `/tmp/shaq-lab-release-20260914`.
No application, account, method, dependency, installed user app, data or credential
changes. No push, GitHub write, installer release, or full native rebuild was performed.

The supplied CI evidence places the intermittent failure at `hdiutil create` after
successful builds/audits, once in bridge DMG creation and once in final DMG creation.
Both formerly made one unguarded create call. This identifies a hosted macOS native
image-creation busy failure class, not the exact underlying runner resource owner.
This task does not claim to identify/kill that owner or cure all native image errors.

Final feeds previously had no public previous package producer, so future deltas
could not be generated there even though internal acceptance produced deltas.
Windows additionally discarded its accepted final feed in the temporary installer
directory; only its Setup/checksum were promoted.

## Changed files

- `packaging/create_dmg.py`: bounded exact-busy retry with named configurable limits,
  per-attempt diagnostics, isolated partial paths, atomic success promotion, unchanged
  original failure status, immediate nonbusy failure, retained timeout diagnostics.
- `packaging/build_macos.sh`, `packaging/installed_update_acceptance.py`: same shared
  DMG helper for final and bridge creation. Existing install/mount/audit flows remain.
- `packaging/release_feed.py`: unauthenticated official public release pagination,
  latest lower same-platform/channel selection, reserved internal-version exclusion,
  canonical repository URLs, existing asset/SHA/size verification, one previous full
  package and minimal feed staged into fresh exact pack output. Channel-specific
  `delta-base.<channel>.json` avoids collisions in merged Mac release assets.
- `packaging/build_desktop.py`: final-only `--prepare-public-base` step before existing
  managed packing; generated feed validation remains unchanged.
- `packaging/windows_delivery.py`: final path passes preparation flag into the actual
  installer output, and only full non-diagnostic acceptance promotes verified feed
  packages/receipt alongside the same accepted installer. `delivery.json` records
  installer SHA-256 and target/channel. Internal acceptance output is never a source.
- `tests/test_dmg_creation.py`, `tests/test_release_feed.py`,
  `tests/test_windows_delivery.py`: regression and caller integration coverage.
- `packaging/README.md`: developer usage and limitations. This report.

## RED / GREEN evidence

RED commands used before the corresponding implementation:

    PYTHONPATH=src python3 -m unittest discover -s tests -p test_dmg_creation.py -v
    PYTHONPATH=src python3 -m unittest discover -s tests -p test_release_feed.py -v
    PYTHONPATH=src python3 -m unittest discover -s tests -p test_windows_delivery.py -v

Observed RED: missing DMG helper; missing public-base producer; unsupported final-only
CLI flag; Windows compile did not request final base preparation. A separate timeout
regression failed because no attempt log survived timeout, then passed after preserving
the captured partial diagnostic. Initial path-comparison assertions were corrected for
macOS `/var` versus `/private/var` canonicalization; no production behavior was weakened.

GREEN focused coverage: busy-then-success, exhaustion, status-one permission denial,
invalid arguments, unrelated busy text, timeout logs, spaces/Unicode, pre-existing final
image preservation, actual final shell/shared bridge command wiring; first managed
release, latest compatible lower release, architecture/digest/size errors, bad source
URLs, advertised 404/503/connect failure, release-list failure, dirty output rejection,
both native final CLI paths; same accepted Windows installer/feed, diagnostic and
upstream-failed nonpromotion. Only OS/HTTP/build boundaries are substituted in tests;
actual caller orchestration, file operations, manifests and validation execute.

Full-suite commands:

    build/release-venv/bin/python -m unittest discover -s tests -v
    zsh -n packaging/build_macos.sh
    git diff --check

The locked native full environment passed **567 tests, 1 platform-conditioned skip**.
The final post-channel-receipt run is retained in `/tmp/shaq-task4e-native-tests-final.log`.
Prior full run log: `/tmp/shaq-task4e-native-tests-approved.log` (567, same skip).
Shell syntax and diff checks passed.

Environment-only failed checks retained transparently: global Python was missing
Zipline (548 tests, 41 import-dependent errors); locked native sandbox run had one
read-only `ps` permission error (565 tests before two caller tests were added).
An approved-permission rerun resolved that environment restriction. No dependency
installation or weakening of those tests occurred.

## Read-only public/native checks and remaining acceptance

Live unauthenticated `prepare_public_base(..., '0.7.0', 'Darwin', 'arm64')` against the
configured official repository returned `first-managed-release`, base null. Therefore
the first managed release is truthfully full-only, not a fabricated public delta.
Future release tests prove verified previous-base staging and correct pack invocation;
they are not evidence of a new real public native delta artifact in this task.

A bounded real fixture used `create_dmg.py` on a small copied README in
`/tmp/shaq-task4e-dmg.jkZNSg`, with a spaces/Unicode source and output. Native create
returned nonbusy `未設定裝置` (device not configured) in the restricted environment,
correctly without retry. No image was produced; subsequent verify consequently failed.
This is **not native acceptance**. No mount, installed app operation, daemon action,
root permission, or broad cleanup was used. Root owns the permission-enabled fixture
and final clean three-platform CI/full installed consecutive-update acceptance.

References supplied/primary-checked by root:
- https://github.com/create-dmg/create-dmg/blob/master/create-dmg (`hdiutil_retry`;
  create busy uses status 1, unlike detach 16; only precise diagnostic is retried).
- https://github.com/actions/runner-images/issues/7522 (hosted macOS busy class).
- https://docs.velopack.io/packaging/deltas (previous release required in outputDir).
