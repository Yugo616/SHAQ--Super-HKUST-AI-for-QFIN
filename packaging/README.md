# Native release builds and acceptance

Build all three artifacts from one clean public source commit. Never publish private
worktree history or ignored build/runtime directories. The workflow is dispatchable on
an acceptance branch and has read-only repository permissions; it does not publish releases.
Version 0.6.0 is reserved for lab-v0.6.0-windows and lab-v0.6.0-macos, after acceptance.

## Build

Use native CPython 3.13, CMake, and the native C compiler. On Windows activate MSVC x64
for PyTables/bcolz and place 64-bit MinGW-W64 GCC on PATH for QuickJS. The workflow configures
both. Python, Git, compilers and Zipline are build-runner tools, not user prerequisites.

    python3.13 -m venv .venv
    # Activate .venv (on Windows use its Scripts/Activate.ps1).
    python packaging/build_native.py
    python scripts/validate_release.py
    python -m unittest discover -s tests -v
    # macOS:
    zsh packaging/build_macos.sh
    # Windows PowerShell:
    # ./packaging/build_windows.ps1

native-sources.json pins original source URLs and hashes. build_native.py builds HDF5,
builds PyTables without optional LZO, repairs native dependencies into relocatable wheels,
installs the exact version closure in requirements.lock.txt, and checks runtime LZO absence.
Windows also genuinely builds bcolz-zipline 1.13.0 and QuickJS 1.19.4 for CPython 3.13.
No assumption of Windows 3.13 wheel existence substitutes for compilation acceptance.
Original source archives, wheel hashes, notices and provenance remain in build artifacts.

NumPy 2.3.5 with pandas 2.3.3 is intentional: NumPy 2.5.3 caused a generic timedelta
deprecation inside pandas and the upstream market calendar. The pinned pair passes
that reproducer without filtering warnings; the minute rules remain unchanged.

The shared payload recipe builds release and Preview. It includes both complete methods,
eight Skills, config/decision assets, native libraries and all three GUI pages.
The .gitattributes LF policy preserves method bytes on Windows. The bundled manifest
records every method file hash and the source commit for cross-platform comparison.

## Installation and acceptance boundaries

- Windows: run the x64 Setup executable. The installer checks official WebView2 registry
  locations and stops with instructions if Evergreen Runtime is absent. Install Microsoft's
  [WebView2 Evergreen Runtime](https://developer.microsoft.com/microsoft-edge/webview2/) and retry.
  Python/Git are not user prerequisites. Windows 10/11 x64 is the support target; the actual
  Windows Server GitHub runner environment is recorded separately and does not prove
  manual acceptance on a clean Windows 10/11 PC.
- macOS: select Apple Silicon or Intel (not universal2), mount the DMG and copy the app.
  The Apple Silicon validation build targets macOS 15 or later; inspect actual native
  minimum versions before declaring a lower baseline or the Intel baseline. Builds carry
  an ad-hoc signature, not Developer ID notarization or a commercial signature.
- Every installer has a SHA-256 sidecar. Keep the old installed Preview and local data
  intact during acceptance; tests copy only into temporary roots.
- Uninstall removes the application, not user research history or credentials.

Installed --smoke --smoke-output PATH runs both deterministic fixture methods through the
real Zipline engine, exact long/reserved-short reconciliation, provisional/final confirmation,
history and ledger reopen. It writes only temporary fixture data and the requested report.
It is not evidence of real model connectivity, Yahoo availability or prediction performance.
--gui-smoke PATH opens an actual native window with an isolated temporary data root,
loads run/editor/history and closes only that test window. Use an external 60-second
timeout; missing native GUI support must be reported, not called a passing check.
Ordinary GUI launch uses normal user data; there is no generic data-home override.

audit_payload.py APP --output REPORT checks the full assembled/installed payload for old
engine/runtime files, LZO dependencies, non-system absolute macOS links, personal builder-home
paths and credential patterns. It accepts the unchanged unsupported LZO stub. Compiler
file-prefix mapping, generated HDF5 build-metadata mapping and ordinary debug-symbol
removal avoid private paths; upstream source and notices remain retained.

CI order: native source build, locked install, tests/eight-Skill validation, complete app,
actual installer install, packaged settlement/reopen, native GUI, payload audit, Mac signature
verification and uninstall. Reports upload even on failure. All three successful jobs with
matching public source/method hashes are required before distribution.
