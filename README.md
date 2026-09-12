# SHAQ Daily Oracle Lab

[简体中文](README.zh-CN.md)

SHAQ Daily Oracle Lab is a local workbench for comparative financial research: it runs governed premarket methods against frozen evidence and compares their records; it is not a live-trading application or investment advice.

![Two frozen runs compared in SHAQ Daily Oracle Lab](docs/assets/run-comparison.png)

## Published installers

The current published internal-test installers are **0.6.1**:

- [Windows 10/11 x64](https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/releases/tag/lab-v0.6.1-windows)
- [macOS 15+ — Apple Silicon or Intel](https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/releases/tag/lab-v0.6.1-macos)

Choose exactly one Mac disk image for the computer's architecture. Each release page includes checksums and an acceptance record and is visibly marked as a prerelease. These research builds are not commercially signed or notarized, so the operating system may require a first-launch confirmation.

The [current `main` source](https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/tree/main) is newer than 0.6.1. It has not yet been accepted as a Windows installer and is not a published upgrade. See [Software updates](docs/software-updates.md) for the exact boundary.

## First comparison in three steps

1. Open **Connection Settings** and connect Codex, Claude Code, an official API, or an OpenAI-compatible relay; then sign in to the team repository and finish the research-data check.
2. On **Start Runs**, select both bundled methods and run them with one model profile. They receive the same frozen evidence and candidate set.
3. On **View Results**, select two records and choose **Compare Selected Runs**. Treat a difference as method evidence only when the model, frozen data, candidates, trading rules, and trade date are all marked the same.

## Three-page workbench

- **Start Runs** — choose a model and method versions, start or resume a batch, and inspect real progress.
- **Edit Versions** — copy a method into a local draft, change its modules, validate it, and explicitly share or download a team version.
- **View Results** — inspect frozen reports, decisions, official direction labels, minute replay, virtual accounts, and controlled run comparisons.

Connection, data, team-sync, and software-update controls open as focused dialogs rather than additional main pages. Raw evidence, model calls, credentials, results, and virtual accounts remain local.

## Two bundled methods

- **Independent Evidence Gate · Formal Baseline** requires aligned applicable domains, independent roots, both context and stock-specific evidence, no independent opposing root, and a maximum of three published directions. Deterministic code makes the final gate decision.
- **Cross-domain Synthesis · Shadow** asks a separate synthesis step to compare the thesis, countercase, unknowns, invalidation conditions, and alternatives over the same frozen domain reports. Code still validates citations, integrity, and the publication cap.

The labels describe research roles, not proven performance. One batch—or any historical replay—cannot establish that either method is superior.

## How the research is kept comparable

```text
point-in-time universe + cutoff-safe sources
                    ↓
       one frozen evidence snapshot
                    ↓
 six blinded domains + non-voting adversary
                    ↓
   selected method's validated decision rule
                    ↓
 local records → later labels and minute replay
```

Every saved run records its method snapshot, model-profile hash, evidence hash, candidate set, trading-rule identity, and date. Missing identity is reported as unknown, never assumed equal. The eight research Skills cover orchestration, market shocks, relationships, primary events, capital flow, derivatives, price/volume structure, and adversarial integrity review; their roles and sources are summarized in [Research and comparison design](docs/research-design.md).

## Documentation

- [User guide](docs/user-guide.md)
- [Research and comparison design](docs/research-design.md)
- [Software updates](docs/software-updates.md)
- [Paper evaluation and virtual accounts](docs/paper-evaluation.md)
- [Third-party notices](docs/third-party-notices.md)
- [Native build and acceptance guide](packaging/README.md)

SHAQ itself remains unlicensed; this repository grants no project license. Third-party components retain their own licenses and notices.
