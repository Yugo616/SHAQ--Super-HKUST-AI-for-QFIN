# Software updates

Software updates and research-method updates are separate operations in SHAQ Daily Oracle Lab.

## Published application version

The current published internal-test installers are **0.6.1**. Their GitHub pages are visibly marked as prereleases:

- [Windows 10/11 x64 release](https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/releases/tag/lab-v0.6.1-windows)
- [macOS 15+ release with separate Apple Silicon and Intel disk images](https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/releases/tag/lab-v0.6.1-macos)

Both pages identify the accepted source revision and provide installer checksums plus `Acceptance.json`. Platform claims stop at the evidence recorded on those pages: Windows acceptance ran on the stated GitHub Windows environment, and the two Mac architectures have separate artifacts. These results do not establish live model connectivity on every user's account.

The [current `main` branch](https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/tree/main) contains newer development source than 0.6.1. It has not yet been accepted as a Windows installer and must not be presented as an installed 0.6.1 update.

## What the app does now

Choose **Software Update (`软件更新`)** in the app header to check the official GitHub release for the current operating system and architecture and read the release notes. When a newer matching version exists, **Download the installer for this computer** opens that full installer asset from the official release. The dialog labels an internal-test release as such; the user still reviews and installs the package.

This is `installer_only` behavior, not automatic installation. This revision does not ship an active in-place or delta updater. It does not download Python files from `main`, overwrite a running application, replace an active research job, or advertise an **Install and Restart** action. If the installed development version is newer than the latest public installer, the dialog reports that state and offers no downgrade button. Installing a newer application must preserve the separate per-user data/configuration directories, but normal backups remain the user's responsibility.

Automatic research scheduling is also separate. Installing or checking for software does not enable a daily run.

## Method updates are different

Use **Edit Versions → Team Sync** to check, download, or upload a research-method package. Team Sync works with immutable packages on the `versions` branch; it does not update the application executable or `main` source. Installing an application update likewise does not silently select, enable, or rewrite a research method.

This separation lets an old application report that a method package is incompatible instead of executing arbitrary remote application code.

## Why there is no small-update button yet

Velopack was evaluated only in an isolated feasibility exercise. The evidence supports further work, not production activation:

- Velopack 1.2.0 is MIT-licensed, supports Python integration, and its macOS ARM64 wheel imported under Python 3.13 on the validation host.
- A tiny ARM64 V1-to-V2 fixture produced a 3,234-byte delta against a 2,916,530-byte full package. Manual reconstruction produced the same extracted V2 file contents and per-file hashes; the ZIP container hash differed because archive metadata/order differed.
- A real 214 MB SHAQ ARM64 `onedir` payload was packaged into an 87 MB full package. Only one SHAQ version was packaged, so this was not a measured SHAQ delta.
- Local tests covered full-package fallback, missing base, a stale partial file, checksum rejection, corrupt delta fallback, corrupt full rejection, and architecture-channel separation.

The exercise did **not** verify Windows x64 or macOS Intel execution, installed-copy apply/restart, replacement of a running app, signing/notarization, a live GitHub/HTTP feed, or a real network interruption. It therefore does not justify exposing in-place update controls.

Primary implementation references: [Velopack Python integration](https://docs.velopack.io/getting-started/python), [channel rules](https://docs.velopack.io/packaging/channels), [delta rules](https://docs.velopack.io/packaging/deltas), and [MIT license](https://github.com/velopack/velopack/blob/develop/LICENSE).

## Safe future path

The first updater-capable bridge release must still be installed as a full package from the existing official 0.6.1-style release pages. Only a later installed version may offer a small update after the complete production gates pass.

Keep the two human-facing release pages per version: one Windows page and one Mac page containing both Mac architectures. Machine update content must be separate for all three runtime identifiers:

- `win-x64-stable`
- `osx-arm64-stable`
- `osx-x64-stable`

Each machine feed must contain only its own full and delta packages. The combined Mac human page cannot act as one shared delta feed for both architectures.

Before an in-place updater can be enabled, one identical public source revision must pass version/architecture identity checks, dependency-notice checks, signed installed V1-to-V2 application tests on all three platforms, corrupt/wrong-feed recovery, active-research and active-settlement exclusion, graceful exit, restart, and per-feed architecture isolation. Until then, the honest action is **check release → read notes → open the official installer**.
