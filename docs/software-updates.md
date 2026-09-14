# Software updates

Application updates and research-method updates are separate. **0.7.0 is the first published managed-update base.** Later patches use a matching public base when available; a patch is available to users only after its installer, update feed and acceptance record appear on the platform release page.

## Platform downloads and acceptance

Primary 0.7.2 downloads: [Windows](https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/releases/tag/lab-v0.7.2-windows), [Apple Silicon / Intel Mac](https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/releases/tag/lab-v0.7.2-macos). Each is available only once its matching packages, checksums and acceptance records appear. Publication requires separate native acceptance for each architecture from the same frozen source, including the actual public Windows 0.7.1 and Mac 0.7.0 upgrade paths. Legacy fallback downloads, not 0.7.2:

- [0.7.1 Windows 10/11 x64 release page](https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/releases/tag/lab-v0.7.1-windows)
- [0.7.0 macOS 15+ release page, separate Apple Silicon and Intel packages](https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/releases/tag/lab-v0.7.0-macos)

Use a page only after its packages, checksums and acceptance record exist. Check each architecture's acceptance record for its exact build source; separately accepted additions may name an explicitly recorded follow-up commit. A candidate branch, CI fixture run or this document is not proof of successful installation on your machine. Native acceptance covers Windows x64, macOS ARM64 and macOS x64 separately. Live personal model connections remain a separate check.

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

Manual route: **Check → Download → Update and Restart**. Downloading alone does not authorize installation. With **Automatic software updates** enabled, checking, downloading and applying when idle are automatic. Both routes preserve local user data and wait for active work before replacement.

The dialog shows current/target version, last check and last successful update when recorded. An update is successful only after the target version has launched and passed native GUI health confirmation. Download completion, a launched installer or a background process alone is not success. If replacement or target startup fails, reopen or reinstall the target full package without deleting user data. Do not assume the old executable remains usable.

## Reliability controls in 0.7.2

In **Connection Settings → Advanced call settings** (`连接设置 → 高级调用设置`), the default total deadline is 600 seconds per model call and transient retry count is 1. Authentication and schema failures are not automatically retried. These are execution controls, not a new model identity or method. The provider collection worker has its own bounded lifetime and closes its owned resources; it does not substitute sources or alter official prices.

For a failed or partial batch, **Resume original batch** (`恢复原批次`) reuses that batch's frozen evidence and valid checkpoints and completes only unfinished calls. It does not collect today's data into yesterday's batch. Late recovery remains research and does not backfill an on-time formal result. Progress refresh preserves selected candidates/versions and expanded details. Windows and macOS automatic research use their native schedulers; enabling software updates remains a separate choice. Closing the normal window does not terminate independently running research.

中文：默认单次模型调用总时限 600 秒，临时故障重试 1 次；认证或格式校验失败不自动重试。「恢复原批次」复用原冻结数据与有效检查点，仅补未完成调用，不补造准时成绩。进度刷新保留选择及展开内容，自动研究支持 Windows 和 Mac 系统调度。更新不更改方法、模型身份、费用、余额或历史记录。

## Method updates are different

Use **Edit Versions → Download (`下载`) / Upload (`上传`)** for the two in-app, multi-select dialogs for immutable method packages on the `versions` branch. Upload lists saved locally owned versions; opening a dialog does not publish anything. This does not change the application executable or `main` source. A software update does not silently select, enable, resize, replay or rewrite a method or historical trade.

## Release engineering boundary

Human-facing Windows and combined-Mac release pages are separate from machine feeds. Feeds are isolated as `win-x64-stable`, `osx-arm64-stable`, and `osx-x64-stable`; the Mac human page is not a shared delta feed. Final release acceptance must test actual installed bridge-to-target download/apply/restart and target GUI health, source/version identity, local data preservation, corrupt/wrong-feed recovery, active-work exclusion and all three architectures. The internal bridge is an acceptance fixture, not an end-user upgrade prerequisite.

Primary references: [Python integration](https://docs.velopack.io/getting-started/python), [channels](https://docs.velopack.io/packaging/channels), [deltas](https://docs.velopack.io/packaging/deltas), and [MIT license](https://github.com/velopack/velopack/blob/develop/LICENSE). These describe the framework, not SHAQ's completed release acceptance.
