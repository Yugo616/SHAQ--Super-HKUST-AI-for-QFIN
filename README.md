# SHAQ Daily Oracle Lab

## 下载与开始

0.6.0 Zipline 升级安装包尚未发布。Windows x64、Mac Apple Silicon 和 Mac Intel 必须通过同一源码版本的实际安装验收后，才会提供两个独立发布页和下载链接；构建定义或本机测试不等于远程平台通过。
内部测试版未进行商业签名；安装和模型连接的实测范围以该发布页说明为准。

1. 安装并打开软件，在「连接设置」连接 Codex、Claude Code 或模型 API。
2. 登录团队 GitHub；软件已配置本仓库，无需填写 Client ID。
3. 在「开始运行」勾选原版基准和综合判断版，点击运行。
4. 在「查看结果」比较方法、股票判断和虚拟账户；点击记录展开分析与简短复盘。

Codex、Claude Code 登录方式要求本机安装对应工具；API 方式不需要。Claude 网页登录不等同 Claude Code。
WorkBuddy没有直接登录接口，只有提供兼容 API 时才通过中转配置使用。研究模式无需富途账户。

## 团队版本库

**[打开统一版本文件夹](https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/tree/versions/shadow_versions)**

程序位于 `main`；团队方法位于 `versions` 分支的 `shadow_versions/作者/版本编号/`。
软件「修改版本 → 团队同步」上传、检查更新和下载。上传只添加完整方法包，不修改程序或旧版本。
没有仓库写权限的成员仍可下载运行。旧个人分支版本继续兼容。

原版和综合判断版随软件提供；同一批次共享数据。模型不同时会标注，不能把全部差异归因于方法。
历史记录、模型调用、每日行情和虚拟账户仅存本机，不上传。

## 结果与虚拟账户

每个方法与模型配置使用独立账户：初始 10,000 美元，每票最多 1,000 美元，整数股，
由 Zipline-reloaded 3.1.1 在真实一分钟数据上回放：参考 09:31 ET 分钟开盘进入、收盘前五分钟开盘退出；半日市按交易日历调整。双边各收 0.05% 手续费，并应用 0.05% 不利滑点。
这是收盘后模拟回放，不是真实券商成交。分钟证据独立复核后才最终结算；下次打开只补齐仍可验证的数据，缺分钟则明确等待或缺失，不猜价、不重复记账。
同时保留相同股数的零成本对照；官方未复权开收盘方向评价单独显示。旧 Backtrader 记录仅保留展示，不重算成新引擎成绩。

简短复盘列出结果和当时依据，不以涨跌结果编造唯一原因。仅有开收盘价格时，不能证明盘中反转或某条消息导致涨跌。
依赖许可见 [第三方说明](docs/third-party-notices.md)。

## Technical overview

SHAQ Daily Oracle Lab is a Windows and macOS research workbench for comparing
governed premarket-analysis Skills. The application freezes one point-in-time
evidence set, gives the same candidates and evidence to every selected Skill
version, and compares their 0–3 open-to-close forecasts locally.

The research mode never imports broker modules and can never submit an order.
The existing Futu simulation operator remains a separate, Mac-only path with a
different setup, runtime directory, system identity and score history.

## What a team member does

```text
Install the app
→ sign in to the team repository in a browser
→ connect an existing Codex / Claude Code login, or add an API profile
→ enter the SEC research identity and verify the PIT universe/local storage
→ check team updates
→ select main and one or more Shadow versions
→ run one shared-evidence comparison
→ inspect the local dashboard
```

The release builds use one code revision for Windows and both macOS architectures. Research mode never
requires a Futu account. Model access can come from a local Codex or Claude Code
login, OpenAI, Anthropic, or an OpenAI-compatible relay API.

Platform-specific installation validation is recorded in each release:

- Windows 10/11 x64
- macOS Apple Silicon
- macOS Intel

The first launch stores model and GitHub credentials in macOS Keychain or
Windows Credential Manager. Credentials, model calls, market evidence and daily
results never enter GitHub. Expiring GitHub App credentials are renewed with the
device-flow refresh token before repository access; the refresh token remains in
the operating-system credential store.

## Research architecture

```text
versioned PIT universe + replaceable data providers
                         │
                         ▼
              one cutoff-safe evidence snapshot
                         │
         ┌───────────────┼────────────────┐
         ▼               ▼                ▼
       main          member Shadow    member Shadow
         │               │                │
         └──── same candidates, model profile and schema ────┘
                         │
                         ▼
 six blind domain analyses → non-voting adversary → sandboxed decision rule
                         │
                         ▼
       local history, comparison, labels and professor export
```

Every batch remembers which data, model and Skill versions were used. Restarting
the app reuses completed work and resumes only unfinished versions; it never
reruns a successful call so that a user can choose a preferred answer. The
technical checks remain available in one collapsed section and do not clutter
the everyday dashboard.

## Eight Skills

| Skill | Role |
|---|---|
| `daily-oracle` | Orchestrates the frozen research question |
| `market-common-shock` | Separates broad market, rates, dollar, credit and volatility shocks |
| `pit-peer-spillover` | Tests industry and economic relationship spillovers |
| `primary-event-reasoner` | Separates new facts, prior expectations, publication time and price absorption |
| `capital-order-flow` | Uses order-flow evidence only when aggressor and depth semantics exist |
| `derivatives-evidence` | Reads implied distributions without mapping Put, Call or OI mechanically to direction |
| `price-volume-structure` | Interprets residual gap, path, participation and liquidity states |
| `thesis-adversary` | Finds leakage, duplicated evidence and horizon mismatch without voting |

Each Skill is a complete three-file package: `SKILL.md`,
`references/foundations.md`, and `agents/openai.yaml`. A Shadow can also include
`decision/decision.js` plus mandatory `decision/cases.json`. The decision rule
runs inside a local QuickJS sandbox and can read only frozen domain conclusions,
the adversary result, and verified evidence roots. It cannot access files,
network, system commands, credentials, broker code, or post-close labels.

The application rejects arbitrary Python, binaries, Actions, credentials, local
paths and runtime data before upload. `main` is read-only; each member publishes
immutable versions to `shadow/<github-login>` and every version stays bound to
its original main commit.

## Models

The same `ModelBackend` contract supports:

- OpenAI Responses API with native strict structured output, `store=false` and
  no tools;
- Anthropic Messages API with structured output and no tools;
- OpenAI Chat Completions-compatible endpoints, with local strict JSON Schema
  validation;
- existing Codex and Claude Code subscriptions through their local logged-in
  command-line clients.

The app probes the exact endpoint, model, authentication style and output mode
before saving a profile. It does not silently switch a model or endpoint after a
401, 429, timeout or schema failure. A relay profile also records its declared
context limit and rejects an evidence packet that would exceed it.

## Data providers

The default research profile uses:

- a versioned S&P 500 membership file for the point-in-time universe;
- FinanceDatabase's public equities dataset for identity and current classification metadata only;
- yfinance for unadjusted bars, premarket observations and basic option surfaces;
- SEC EDGAR for primary filings and their publication times;
- an optional OpenBB REST profile that can replace declared capabilities.

FinanceDatabase cannot determine historical index membership. Missing order-book
or aggressor semantics makes the capital domain unavailable. A basic option
surface may describe implied move, skew and term structure but cannot invent a
directional option-flow vote. Free data is explicitly marked research-only.

## Local dashboard and scientific labels

The dashboard shows the timeline, selected versions, all candidates, six domain
reports, support and counterarguments, unknowns, invalidation conditions,
adversary result, decision-code reason and open-to-close results. File hashes and
technical metadata are kept behind a collapsed verification section. It does not
display or claim to reconstruct a model's hidden chain of thought.

The research label is the official unadjusted US regular-session open-to-close
return. The first observation is provisional; a later independent read must match
before the label becomes final. Exact flat closes are neutral and count as wrong
for a directional forecast.

## Team repository setup

The team administrator registers one GitHub App with Device Flow enabled and
provides its public Client ID in `config/team-repository.json` or the first-launch
screen. The app requests repository metadata read and the minimum contents access
needed to read versions and, for members with write permission, append a version
to their personal branch. Read-only members can install and run team versions but
cannot upload.

## Developer verification

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python3 packaging/build_native.py
python3 scripts/validate_release.py
python3 -m unittest discover -s tests -v
shaq-daily-oracle-desktop --smoke
```

The GitHub workflow defines native Windows x64, macOS arm64 and macOS x86_64 builds;
only successful installed-artifact runs establish platform acceptance. See
[native build and installation instructions](packaging/README.md). Installers must contain no account identifier, email address, API key,
daily run record or development-machine path. Internal packages use SHA-256 files
and ad-hoc or temporary signing, so the operating system may ask the reviewer to
confirm the first launch.

No software license is granted in this internal review version.
