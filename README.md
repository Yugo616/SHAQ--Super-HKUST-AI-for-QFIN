# SHAQ Daily Oracle Lab

[简体中文](README.zh-CN.md)

SHAQ Daily Oracle Lab is a local workbench for comparative financial research: it runs governed premarket methods against frozen evidence and compares their records; it is not a live-trading application or investment advice.

The results view separates **method changes, model identity, frozen data, candidate selection, and trading rules** before comparing predictions. A mismatch or missing record remains visible; it is not presented as a controlled experiment.

## 0.7.0 platform release pages

This source is the **0.7.0 release candidate**. The platform pages below must supply the matching packages, source tag, checksums and `Acceptance.json` before they are usable downloads; this document does not claim completed acceptance or publication.

- [0.7.0 Windows 10/11 x64](https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/releases/tag/lab-v0.7.0-windows)
- [0.7.0 macOS 15+ — Apple Silicon or Intel](https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/releases/tag/lab-v0.7.0-macos)

Choose exactly one Mac disk image for the computer's architecture. Each release page includes checksums and an acceptance record and is visibly marked as a prerelease. These research builds are not commercially signed or notarized, so the operating system may require a first-launch confirmation.

Existing **0.6.1/0.6.2** installations require a first full install of the managed application; they cannot acquire the native updater through a small update. Close the old app, install the matching full package, then open the new app. If a legacy copy occupies a different location, remove only that old program through the operating system's Apps interface, not SHAQ user data. Local records, balances, settings and method drafts remain separate. See [Software updates](docs/software-updates.md) for GUI-only steps. Platform fixture checks do not verify each user's live model connection.

## First comparison in three steps

1. Open **Connection Settings** and connect Codex, Claude Code, an official API, or an OpenAI-compatible relay; then sign in to the team repository and finish the research-data check.
2. On **Start Runs**, select both bundled methods and run them with one model profile. They receive the same frozen evidence and candidate set.
3. On **View Results**, select two records and choose **Compare Selected Runs**. Treat a difference as method evidence only when the model, frozen data, candidates, trading rules, and trade date are all marked the same.

## Three-page workbench

### Three connection entries

- **Connect Codex** detects a usable, signed-in Codex CLI and tests a structured response. Merely installing the chat app is not sufficient. [Official CLI setup](https://learn.chatgpt.com/docs/codex/cli).
- **Connect Claude Code** uses the Claude Code CLI and its login, not the Claude chat website/app login.
- **Configure API** accepts an official OpenAI/Anthropic protocol or compatible relay: enter its HTTPS base URL, API key, exact model ID and supported context limit. API-only users need neither CLI.

Relay model ID, key group/permissions and protocol must match the provider's live catalog. The connection test shows observed HTTP status, bounded service code/message and request ID when available. HTTP 400 alone does not establish a cause: check URL, protocol, model and key permissions. SHAQ never silently switches provider or model. See [connection details](docs/user-guide.md#1-connect-an-analysis-model).

Today uses New York time and the NYSE calendar: closed dates and times before 04:00 ET are blocked, including Hong Kong Monday mornings still Sunday in New York. A regular NYSE Monday can run with current-day premarket stock observations. If all stocks lack them, collection stops before model analysis; empty data is not proof of no trading or API failure. Partial coverage remains explicit. Late weekday research is not formal premarket performance, and saved historical replay remains separate.

### Main pages

- **Start Runs** — choose a model and method versions, start or resume a batch, and inspect real progress.
- **Edit Versions** — copy a method into a local draft, change its modules, validate it, and explicitly share or download a team version.
- **View Results** — inspect frozen reports, decisions, official direction labels, minute replay, virtual accounts, and controlled run comparisons.

Connection, data, team-sync, and software-update controls open as focused dialogs rather than additional main pages. Raw evidence, model calls, credentials, results, and virtual accounts remain local.

## Two bundled methods

- **Independent Evidence Gate · Formal Baseline** requires aligned applicable domains, independent roots, both context and stock-specific evidence, no independent opposing root, and a maximum of three published directions. Deterministic code makes the final gate decision.
- **Cross-domain Synthesis · Shadow** asks a separate synthesis step to compare the thesis, countercase, unknowns, invalidation conditions, and alternatives using its validated domain reports and adversary review. Both methods receive the same frozen evidence; their reports need not be identical. Code still validates citations, integrity, and the publication cap.

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
