# Software updates

Application updates and research-method updates are separate. This source is the **0.7.0 release candidate**: implementation/test results do not mean an installer or native update has been accepted and published.

## Platform downloads and acceptance

- [0.7.0 Windows 10/11 x64 release page](https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/releases/tag/lab-v0.7.0-windows)
- [0.7.0 macOS 15+ release page, separate Apple Silicon and Intel packages](https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/releases/tag/lab-v0.7.0-macos)

Use a page only after its packages, checksums and `Acceptance.json` exist. The release tag identifies the exact tested source; checksum/acceptance assets identify installers. A candidate branch, CI fixture run or this document is not proof of publication or successful installation on your machine. Native acceptance must cover Windows x64, macOS ARM64 and macOS x64 separately at the release's exact source revision. Live personal model connections remain a separate check.

## First upgrade from legacy 0.6.1 / 0.6.2

Legacy unmanaged applications cannot install the new updater through an in-place or delta update. Use the full package once:

1. Save unfinished method edits and close all old SHAQ windows. Allow active analysis/settlement to finish first.
2. Download the full installer matching the computer from the platform page and check its checksum.
3. Install the managed application. On macOS, follow the disk image's application installation instructions. On Windows, open the full installer and follow its prompts.
4. If the legacy copy uses a different installation location, remove **only the old SHAQ program** through Windows **Settings → Apps → Installed apps** or macOS **Finder → Applications → Move to Trash** before starting the new copy. Do not remove SHAQ's separate user-data/configuration folders or use a cleanup utility to erase associated data.
5. Open the newly installed SHAQ, check the version in **Software Update (`软件更新`)**, and confirm that local records, balances, saved methods/drafts and settings are visible.

Old program files may be replaced/removed before the target application starts. There is no promise of a retained rollback copy. Research records, balances, method drafts and settings live outside the installation and must remain; normal backups remain prudent. The [Velopack preservation guidance](https://docs.velopack.io/integrating/preserved-files) explains why mutable user data must not depend on replaceable program files.

中文首次升级步骤：保存草稿并等待任务结束 → 关闭旧版 → 从平台页下载完整包并安装 → 如旧版在另一位置，通过系统「应用」界面只移除旧程序 → 打开新版并检查版本、记录、余额、方法与设置。不要删除用户资料目录；不承诺保留旧程序回滚副本。

## Subsequent managed native updates

Open **Software Update** in the app header. A managed, supported installation can check the official release for its operating system and architecture, download a verified update, and offer **Update and Restart**. Matching deltas are preferred when available; a verified full package is the fallback. Missing/corrupt packages or the wrong architecture/feed must fail safely rather than install unverified content. Source and unmanaged installs retain installer-only guidance.

Downloads do not end active work. Apply waits for analysis, settlement and background writes to finish; unsaved GUI edits must be resolved before restart. Cross-process runtime admission prevents an old window or scheduled worker from resuming writes after installation. A queued manual update can be cancelled before installation begins. Automatic software updates are a separate saved preference and remain off until explicitly enabled; turning them on does not enable automatic research.

The dialog shows current/target version, last check and last successful update when recorded. An update is successful only after the target version has launched and passed native GUI health confirmation. Download completion, a launched installer or a background process alone is not success. If replacement or target startup fails, reopen or reinstall the target full package without deleting user data. Do not assume the old executable remains usable.

## Method updates are different

Use **Edit Versions → Download (`下载`) / Upload (`上传`)** for the two in-app, multi-select dialogs for immutable method packages on the `versions` branch. Upload lists saved locally owned versions; opening a dialog does not publish anything. This does not change the application executable or `main` source. A software update does not silently select, enable, resize, replay or rewrite a method or historical trade.

## Release engineering boundary

Human-facing Windows and combined-Mac release pages are separate from machine feeds. Feeds are isolated as `win-x64-stable`, `osx-arm64-stable`, and `osx-x64-stable`; the Mac human page is not a shared delta feed. Final release acceptance must test actual installed bridge-to-target download/apply/restart and target GUI health, source/version identity, local data preservation, corrupt/wrong-feed recovery, active-work exclusion and all three architectures. The internal bridge is an acceptance fixture, not an end-user upgrade prerequisite.

Primary references: [Python integration](https://docs.velopack.io/getting-started/python), [channels](https://docs.velopack.io/packaging/channels), [deltas](https://docs.velopack.io/packaging/deltas), and [MIT license](https://github.com/velopack/velopack/blob/develop/LICENSE). These describe the framework, not SHAQ's completed release acceptance.
