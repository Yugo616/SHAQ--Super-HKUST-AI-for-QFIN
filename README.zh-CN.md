# SHAQ Daily Oracle Lab

### 在同一份市场证据上，比较不同的 AI 投资研究方法

[English](README.md) · [下载软件](#下载软件) · [第一次使用](#第一次使用) · [团队方法库](https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/tree/versions/shadow_versions) · [详细手册](docs/user-guide.md)

**修改方法 → 同数据运行 → 看清判断与结果的变化。**

SHAQ 把六个专业研究领域、反方审查和无需券商的虚拟账户放进一个本地工作台。组员可以修改方法，同时运行基准版和自己的版本，再逐层查看每个预测如何产生。

- **能对照：** 分开记录方法、模型、数据与交易规则，不把所有差异归功于新方法。
- **看得见：** 运行中查看已完成的领域报告、反向证据与最终取舍。
- **留本机：** 每日证据、模型调用、草稿、结果与余额保存在自己的电脑；GitHub 只共享方法版本。

## 下载软件

| 你的电脑 | 下载入口 |
|---|---|
| Windows 10/11，x64 | [Windows 安装程序](https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/releases/tag/lab-v0.7.0-windows) |
| M 系列 Mac，macOS 15 或以上 | [Apple Silicon DMG](https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/releases/tag/lab-v0.7.0-macos) |
| Intel Mac，macOS 15 或以上 | [Intel DMG](https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/releases/tag/lab-v0.7.0-macos) |

普通用户只下载 **`.exe` 或 `.dmg`**。其余文件供自动更新、校验和第三方许可使用，不必逐个下载。不需要另外安装 Python、Git、富途或开通券商账户；Windows 安装程序会检查 WebView2。当前为内部测试发布，尚无商业签名或公证；首次打开遇到系统来源提示时，请核对下载页与校验值。

Windows 和 M 系列 0.7.0 来自同一[源码提交 `56d6467`](https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/commit/56d64679bb8a9291e229e29743c9be29a7a16517)。Intel 0.7.0 使用修正原生验收等待逻辑的[后续提交 `97384db`](https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/commit/97384db94f4ccf1c85b046c4c82bc170f0c9a8d0)。各平台附有对应验收记录与校验值，不替换旧附件。

## 第一次使用

### 1. 安装并打开

Windows 双击安装程序，跟随提示完成安装。Mac 打开 DMG，按窗口指引安装后，再打开已安装的软件，不要一直从磁盘映像里运行。第一次会进入设置向导；以后可从右上角「连接设置」重新打开。

### 2. 连接一个模型，三选一即可

| 选择 | 需要准备什么 | SHAQ 实际连接什么 |
|---|---|---|
| **连接 Codex** | 本机可调用的 Codex CLI，并完成该 CLI 的登录 | 本机程序，不需在 SHAQ 再填 API Key |
| **连接 Claude Code** | 本机可调用的 Claude Code CLI，并完成登录 | 本机程序，不是 Claude 聊天窗口 |
| **连接 API** | 官方 API 或中转站账号 | Key、模型；中转另填 URL 和上下文长度 |

**只有中转站 API 也能用，不必再安装 Codex 或 Claude。** 聊天会员和 API 余额不是同一种产品。WorkBuddy 如提供兼容 API，可走第三个入口；SHAQ 不直接控制其桌面界面。

找到程序不代表已经登录。若提示未登录，先完成对应 CLI 的浏览器登录，再重新测试。只有模型真正返回合格结果后才保存连接；失败不会替换原来可用的配置。参考：[Codex 登录](https://learn.chatgpt.com/docs/auth)、[Claude Code 安装与登录](https://code.claude.com/docs/en/setup)、[连接排错](docs/user-guide.md#1-connect-an-analysis-model)。

### 3. 登录团队 GitHub，检查数据

点击「登录 GitHub」，在浏览器确认；填写团队的 SEC 研究身份，即团队名称及可联系的邮箱，再运行数据与本地空间检查。GitHub 登录用于分享方法，不用于连接模型。上传需要团队仓库的写入权限。

### 4. 同时运行两个方法，打开结果

在「开始运行」选择一个模型，勾选两个内置方法，点击「运行选中版本」。程序自动采集并冻结数据，再分析。展开当日进度可查看已完成报告；结束后到「查看结果」，点击一行回放全过程，或选择两条记录进行比较。

软件按纽约交易日历运行。休市、纽约时间凌晨 04:00 以前、或没有当天盘前行情时会提示并停止分析。北京时间周一早上可能仍是纽约周日。错过盘前窗口的研究记录可以查看，但不混入按时预测成绩。自动运行需要自行开启，电脑须保持清醒、联网。

## 六个专业领域如何协作

```mermaid
flowchart LR
    A[市场数据与一手公告] --> B[冻结同一批证据]
    B --> C[六个专业领域]
    C --> D[反方审查]
    D --> E[所选方法的最终决策]
    E --> F[0–3只方向预测]
    F --> G[收盘后价格与分钟回放]
    G --> H[本机结果对照与账户历史]
```

| 领域 | 主要回答什么 | 研究基础 |
|---|---|---|
| 市场环境 | 是否是大盘、利率等共同冲击？为什么可能继续影响日内？ | Cieslak–Pang；Lou–Polk–Skouras |
| 行业与关系传导 | 行业、客户、供应商或竞争者的信息如何传导？ | Hoberg–Phillips；Cohen–Frazzini |
| 公司催化事件 | 新事实是什么？与预期和已经涨跌的部分有何区别？ | Jiang–Li–Wang；Glasserman–Lin |
| 买卖压力与流动性 | 买卖压力是否持续？价格是否真的跟着变化？ | Cont–Kukanov–Stoikov |
| 期权定价与仓位线索 | 期权反映什么波动范围、风险和可靠仓位线索？ | Pan–Poteshman；Cremers–Weinbaum |
| 价格走势与参与度 | 价格路径与成交参与呈现什么状态，而非套用技术口诀？ | Murray–Xia–Xiao；Sullivan–Timmermann–White |

每个领域输出主要判断、支持证据、最强反方、未知项和失效条件。反方寻找遗漏、矛盾及重复证据。界面展示真实保存的报告，不伪造模型内部思维链，也不事后补写漂亮理由。具体论文、采用方式与边界保存在各个 [Skill](skills/) 的研究依据中，另见[研究设计](docs/research-design.md)。

## 两个方法，区别在哪里

| | 独立证据门禁版 · 正式基准 | 跨域综合研判版 · Shadow |
|---|---|---|
| 谁作最终决定 | 程序按独立证据与同向确认规则检查 | 综合步骤对照六域与反方，解释最终取舍 |
| 如何对待分歧 | 独立反向证据阻止发布 | 必须回应反方，但不因观点不同自动否决 |
| 共同要求 | 有效证据、截止时间、可核验引用、最多三只 | 同样的完整性边界与数量上限 |

正式基准／Shadow 是实验身份，不表示哪一个已经更赚钱。Lab 中两者都是研究模拟。只有模型、数据、候选和账户规则一致时，才适合把差异归因于方法修改；比较页会标出不一致的部分。

## 修改、分享与比较

1. **开始运行：** 选模型与版本，立即运行或设置自动运行，查看当日研究进度。
2. **修改版本：** 建立本地草稿，编辑分析方法、研究依据、角色或支持的规则代码，保存为新版本。「下载」「上传」分别打开多选弹窗；内容哈希避免重复传输。
3. **查看结果：** 看预测、正确／错误与虚拟余额；点击一天，查看筛选、每只股票的六域报告、反方与最终决策。

团队方法来自 [`versions` 分支](https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/tree/versions/shadow_versions)，软件来自 Release。下载方法不需要重装软件；更新软件不会改写方法快照或历史预测。

## 数据与虚拟账户

默认使用 **Yahoo/yfinance、SEC EDGAR、版本化指数成分和 FinanceDatabase 证券资料**。OpenBB REST 为可选连接，不是内置的免费全能数据库。免费来源没有的盘口、可靠期权主动方向及完整公司关系会显示缺失，不补造数据。

不连接券商也能记账：收盘后，由 **Zipline** 根据冻结预测及规定分钟价格回放，按保存的仓位规则计算股数、手续费和不利滑点。方向对错仍按常规盘开盘→收盘评价。缺少必要分钟时保留未完成状态，不伪造零股交易。成交明确标注「收盘后模拟回放」，不是盘中真实成交。兼容账户序列继承余额；不兼容的方法、模型或规则分开统计。详见[账户与评价规则](docs/paper-evaluation.md)。

## 软件更新

右上角「软件更新」支持手动检查与可选自动更新。下载后，等待任务结束并处理未保存编辑，再替换程序；界面显示版本与更新时间。余额、历史和草稿保存在独立用户数据目录。

旧 0.6.x 用户需先完整安装一次 0.7.0 或更新版本。此后有匹配差分包时减少下载量，没有时使用校验过的完整包。「修补版」不代表所有平台每次都能小包更新。[更新说明](docs/software-updates.md)

## 给组员与代码助手的导航

先读[使用手册](docs/user-guide.md)，再按下表定位。不要把每日数据当成源码上传，也不要修改冻结预测来让测试通过。

| 要做什么 | 从哪里看 |
|---|---|
| 理解专业角色 | `skills/<skill>/SKILL.md`、`references/foundations.md`、`agents/openai.yaml` |
| 理解研究如何运行 | `src/shaq_daily_oracle/lab_service.py` 及其研究流程调用 |
| 适配模型 | `src/shaq_daily_oracle/model_backends.py` 与兼容性测试 |
| 修改界面 | `src/shaq_daily_oracle/desktop/` 与 UI 回归测试 |
| 理解安装与更新 | `packaging/README.md`、`docs/software-updates.md` |
| 验证修改 | `tests/`、`scripts/validate_release.py`；原生安装验收另记 |

组织方式参考 [TradingAgents](https://github.com/TauricResearch/TradingAgents)、FinCon 和 InvestorBench。SHAQ 重点是让团队修改方法、做同证据对照，而不是增加更多投票角色。研究引用是设计依据，不是本系统已盈利的证明。

本项目用于研究，不提供投资建议或真实交易服务。SHAQ 暂未添加项目许可证；第三方保留各自的[许可与声明](docs/third-party-notices.md)。
