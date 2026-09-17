# Software updates

## 0.7.71: Windows settlement recovery

Windows-only installer/update release; macOS remains on 0.7.7. Retain the last
saved settlement while observations are rechecked, shorten index write
transactions, and retry transient database locks. Old failed runs stay
recoverable in history instead of appearing as today's progress. Unresolved
default model profiles require explicit selection. Existing Windows application
shortcuts are repaired against the running installation, not a saved username.

Predictions, account rules and method packages are unchanged. Build provenance
is included in the installed `third-party/manifest.json` resource (under the
PyInstaller resource directory); it contains the source commit and dependency
identity, not personal run data.

## 0.7.7: grouped results and continuous balances

- Select a whole date/version run once; stock rows show their own net result, while the group header shows the day's total and balance. The balance chart includes compatible, counted historical and forward records without resizing old trades.
- Show recorded response/requested model names instead of internal placeholders. Optional local historical annotations affect display only; no user's model name or account records ship with the application.
- Research evidence must be frozen by 08:50 ET and inference completed strictly before the exchange session opens. Preserve original predictions and timestamps and store the new eligibility assessment separately. The independent broker operator schedule is unchanged. See [timing and replay policy](research-timing.md).
- Missing minutes are not automatically replaced. Explicit, local, hash-bound historical exceptions retain their real reference minute and provenance, and do not authorize fallback for other records.
- Build preflight verifies the managed updater runtime before packaging. The two method packages, sizing, fees and saved forecasts are unchanged.

## 0.7.6: native model dropdown

The model catalog now uses a native dropdown instead of browser-dependent input suggestions. Choose a returned model or type an explicit identifier, then test and save. This fixes the missing suggestion popup in the Mac desktop window. Provider discovery, saved model policy, predictions and account rules are unchanged from 0.7.5.

## 0.7.5: choose and retain an explicit analysis model

- Open **Connection Settings** at any time, choose Codex, Claude Code or API, then choose a model. Local choices come from the installed tool's metadata interface, not a baked-in model list. If a tool or gateway cannot list models, enter a concrete identifier and test it. Reading a list does not run inference; **Test and save** does.
- Successful saves apply to future manual and scheduled runs without enabling automation. Running batches and frozen records keep their original profile. A failed connection test keeps the working configuration. API keys can be reused only for the same protocol, URL and authentication style.
- Legacy `subscription-default` connections require one explicit selection before a new run. Calls always pass the selected local model. The requested identifier is not presented as provider-confirmed; Claude's returned model metadata is retained when available. No historical model identity is invented.
- 0.7.4 is already public and remains immutable. This patch contains its balance-status, minute-retry and terminal-progress fixes. Methods, fees, sizing and past predictions are unchanged. No private model account is used by automated release tests.

References: [Codex model/list](https://developers.openai.com/codex/app-server/), [Claude Code model configuration](https://code.claude.com/docs/en/model-config), [Claude SDK metadata handshake](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/_internal/query.py).

## 0.7.4: explain balances and recover missing settlement data

- Daily results explain whether a record contributed to the balance, is waiting for minute data, or is a late research replay. A completed direction label does not imply a completed account settlement.
- Balance cards identify the latest included date. Internal model placeholders are hidden; replay details show a recorded model identifier only when one exists.
- **补取缺失行情** refreshes minute observations for the selected date and reconciles accounts without rerunning models or daily labels. Missing target minutes are not replaced with adjacent bars or daily opens. Late replays remain excluded from forward balances.
- Finished and failed progress bars stop animating, including older records without task totals.
- No method packages, sizing rules, fees, frozen predictions or account eligibility rules change. FINRA/Cboe sample research is not a new production data provider.

Application updates and research-method updates are separate. **0.7.0 is the first published managed-update base.** Later patches use a matching public base when available; a patch is available to users only after its installer, update feed and acceptance record appear on the platform release page.

## Platform downloads and acceptance

Primary 0.7.7 downloads: [Windows](https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/releases/tag/lab-v0.7.7-windows), [Apple Silicon / Intel Mac](https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/releases/tag/lab-v0.7.7-macos). Each is available only once its matching packages, checksums and acceptance records appear. Publication requires separate native acceptance for each architecture from the same frozen source, including an upgrade from the matching public base recorded in its acceptance report. Previous downloads:

- [0.7.6 Windows release page](https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/releases/tag/lab-v0.7.6-windows)
- [0.7.6 macOS release page](https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/releases/tag/lab-v0.7.6-macos)

- [0.7.4 Windows release page](https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/releases/tag/lab-v0.7.4-windows)
- [0.7.4 macOS release page](https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/releases/tag/lab-v0.7.4-macos)

- [0.7.3 Windows release page](https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/releases/tag/lab-v0.7.3-windows)
- [0.7.3 macOS release page](https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/releases/tag/lab-v0.7.3-macos)

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

## Reliability controls in 0.7.3

This patch separates completed methods from failed methods in the same batch. Failed-method checkpoints must still match their original frozen inputs and output contract. Invalid synthesis citations receive a specific error and returned rejected reports are preserved outside successful caches. Genuine transient data failures have a bounded retry; **Retry failed items** (`重试失败项`) refreshes only failed price tasks, without calling the model again. Account history and prediction rules remain unchanged.

The main progress view now shows a real task-count bar per method. Expand a method, then a stock/domain to read a completed report. It does not expose long technical timelines or invent model reasoning. The result view shows method balances and per-stock prediction, open, close and open-to-close change; accounting calculations remain stored but duplicate cost and one-share comparison panels are no longer displayed. Background refresh preserves the position and selection the user is currently reading.

中文：同批中失败的方法不再遮住成功的方法；暂时性行情错误有界重试，也可点「重试失败项」。进度按版本显示真实完成数量，展开股票和领域查看已保存分析。结果只展示版本余额、股票方向及开收涨跌，不显示重复的成本和一股对照面板。额度耗尽或引用不合格仍明确提示，不能通过反复重试掩盖。

In **Connection Settings → Advanced call settings** (`连接设置 → 高级调用设置`), the default total deadline is 600 seconds per model call and transient retry count is 1. Authentication and schema failures are not automatically retried. These are execution controls, not a new model identity or method. The provider collection worker has its own bounded lifetime and closes its owned resources; it does not substitute sources or alter official prices.

For a failed or partial batch, **Resume original batch** (`恢复原批次`) reuses that batch's frozen evidence and valid checkpoints and completes only unfinished calls. It does not collect today's data into yesterday's batch. Late recovery remains research and does not backfill an on-time formal result. Progress refresh preserves selected candidates/versions and expanded details. Windows and macOS automatic research use their native schedulers; enabling software updates remains a separate choice. Closing the normal window does not terminate independently running research.

中文：默认单次模型调用总时限 600 秒，临时故障重试 1 次；认证或格式校验失败不自动重试。「恢复原批次」复用原冻结数据与有效检查点，仅补未完成调用，不补造准时成绩。进度刷新保留选择及展开内容，自动研究支持 Windows 和 Mac 系统调度。更新不更改方法、模型身份、费用、余额或历史记录。

## Method updates are different

Use **Edit Versions → Download (`下载`) / Upload (`上传`)** for the two in-app, multi-select dialogs for immutable method packages on the `versions` branch. Upload lists saved locally owned versions; opening a dialog does not publish anything. This does not change the application executable or `main` source. A software update does not silently select, enable, resize, replay or rewrite a method or historical trade.

## Release engineering boundary

Human-facing Windows and combined-Mac release pages are separate from machine feeds. Feeds are isolated as `win-x64-stable`, `osx-arm64-stable`, and `osx-x64-stable`; the Mac human page is not a shared delta feed. Final release acceptance must test actual installed bridge-to-target download/apply/restart and target GUI health, source/version identity, local data preservation, corrupt/wrong-feed recovery, active-work exclusion and all three architectures. The internal bridge is an acceptance fixture, not an end-user upgrade prerequisite.

Primary references: [Python integration](https://docs.velopack.io/getting-started/python), [channels](https://docs.velopack.io/packaging/channels), [deltas](https://docs.velopack.io/packaging/deltas), and [MIT license](https://github.com/velopack/velopack/blob/develop/LICENSE). These describe the framework, not SHAQ's completed release acceptance.
