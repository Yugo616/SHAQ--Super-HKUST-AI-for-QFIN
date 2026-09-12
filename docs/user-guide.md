# User guide

SHAQ Daily Oracle Lab is a local comparative-research workbench. Its research mode does not import broker modules or submit orders. A direction label, paper replay, and virtual-account result are three different records.

## Install

The published 0.6.1 internal-test downloads are:

- [Windows 10/11 x64 installer](https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/releases/tag/lab-v0.6.1-windows)
- [macOS 15+ Apple Silicon and Intel disk images](https://github.com/Yugo616/SHAQ--Super-HKUST-AI-for-QFIN/releases/tag/lab-v0.6.1-macos)

On Mac, choose the disk image matching the computer's processor. On Windows, WebView2 Evergreen Runtime is required; the installer identifies a missing runtime before installation. Python and Git are not end-user prerequisites. The GitHub pages mark these builds as prereleases, and they are not commercially signed or notarized, so review the release page, checksum, and operating-system warning before first launch.

The release acceptance record covers deterministic fixtures and native build checks. It does not prove that every personal API, local subscription login, market-data request, or computer configuration works.

## First setup

Open **Connection Settings** from the app header and complete the four cards.

### 1. Connect an analysis model

Choose one route:

- **Codex** uses the Codex CLI already installed and logged in on this computer.
- **Claude Code** uses the Claude Code CLI already installed and logged in. A Claude website or desktop-chat login is not the same login.
- **OpenAI API** and **Anthropic API** take the model ID and API key.
- **Compatible relay** takes its explicit base URL, model ID, context limit, and API key.

On Windows, local-subscription discovery accepts the provider's native `.exe` or `.com` CLI. Shell launchers are not accepted because they do not meet the desktop process boundary; an API profile remains available instead. Use the official installation links in the dialog when a native CLI is absent.

The app performs a small structured-response probe before saving a profile. A malformed response, authentication error, rate limit, timeout, or schema failure is shown with **Retry**, **Edit Connection**, and **Copy Error** actions. It does not silently switch provider, endpoint, or model. Ordinary setup has no temperature field; each protocol sends only parameters supported by that profile.

API secrets and GitHub credentials go to macOS Keychain or Windows Credential Manager. Non-secret profile metadata is stored in the app's per-user configuration directory.

### 2. Sign in to the team repository

Choose **Sign in to GitHub** and finish the browser device flow. The repository application ID is already configured. Read-only members can download team methods; uploading requires repository write permission.

Software source and method versions are deliberately separate. Team methods live on the `versions` branch. Use **Edit Versions → Team Sync** to upload or download them; this never upgrades the application.

### 3. Enter the SEC research identity

Provide a real team name and monitored contact email for the SEC request user agent. This identity is part of responsible data access; it is not published with research records.

### 4. Check data and local storage

The default profile checks the versioned point-in-time universe and the app's local research directories. It uses FinanceDatabase only for identity/current classification metadata, yfinance for configured market observations, SEC EDGAR for primary filings, and optionally an explicitly configured OpenBB REST service. Missing data stays missing; the app does not convert a provider failure into a neutral vote.

## Run the first comparison

1. On **Start Runs**, select **Independent Evidence Gate · Formal Baseline** and **Cross-domain Synthesis · Shadow**. Select one model profile for both.
2. Choose **Run Selected Versions**. The batch collects once, freezes the candidate set and evidence, then runs both methods against those same inputs.
3. On **View Results**, check two run rows and choose **Compare Selected Runs**.

The comparison checks six dimensions: method snapshot, model-profile hash, frozen-evidence hash, candidate-set hash, simulation-rule identity, and trade date. A pure method interpretation requires every non-method dimension to be the same and the method snapshots to be known. A missing old field is **Unknown**, not **Same**. When a non-method dimension differs, the runs remain visible but the outcome difference cannot be attributed only to the method.

## The three pages

### Start Runs

Select method versions and a model, request a cost estimate when price metadata exists, and start the batch. Progress comes from saved collection and analysis events: candidate intake, six domains, adversary review, decision, and later result refresh. The interface preserves the selected stock and open detail sections while a job is active.

After an interruption, retry the failed version from its saved batch. Completed valid work is reused; a retry does not rerun a successful model call merely to seek a preferred answer.

Automatic runs are off until explicitly enabled. Their method selection is separate from the manual selection. The computer must remain on and online at the displayed local time.

### Edit Versions

Choose a base version and **Copy as New Version** before editing. A method package contains the eight Skill documents, agent metadata, references, module code/tests, and decision code/tests. Validation rejects arbitrary Python, binaries, workflow files, credentials, runtime data, and local paths. Decision JavaScript runs in a restricted local sandbox without file, network, shell, credential, broker, or result-label access.

Saving creates a local immutable method version. **Team Sync** is a separate explicit action. It adds a complete package without overwriting earlier versions or application source.

### View Results

Filter by date, method, and model. Open a row to inspect candidates, supplied domain reports, strongest countercase, unknowns, invalidation conditions, adversary result, decision reason, and evidence identifiers. The interface presents stored conclusions and provenance; it does not claim to reveal a model's hidden chain of thought.

Use the checkboxes to compare any two frozen records. The comparison includes method-file differences, stock-level directions/rejections, and the separately recorded after-close replay.

## Labels, replay, and accounts

### Official direction label

The scientific label is the unadjusted US regular-session open-to-close return. The first eligible observation immediately appears as provisional; a later read must match before it is marked confirmed, without requiring a new trading day. An exactly flat close is neutral and is wrong for either a bullish or bearish forecast.

### Minute replay

Zipline-reloaded 3.1.1 performs brokerless after-close replay from the frozen directions. The default account begins at USD 10,000, limits each prediction to USD 1,000, and uses integer shares. Entry uses the 09:31 ET minute open; exit uses the minute open five minutes before the calendar close, including the adjusted time on half days. Each side applies a 0.05% commission and 0.05% adverse slippage.

An explicitly activated experimental continuity policy can instead size from the 20 prior eligible trading days: `opening equity × 0.2% ÷ frozen volatility`, capped at 10% per symbol and 30% gross exposure. The interface labels that policy experimental; it is not an optimality claim. If the required point-in-time volatility is missing or invalid, that symbol is not sized by guessing.

Minute observations are independently re-read before final confirmation. Missing entry or exit minutes are not replaced with daily bars or forward-filled prices. Reopening the app can fill still-verifiable incomplete records, but does not promise that a provider will retain old minute data. Duplicate refreshes do not double-book a result; a changed provider observation is recorded as a revision rather than silently rewriting confirmed profit and loss.

### Account boundaries

Method, model, engine, and rule identities define separate accounts. Prospective continuous accounts, historical replay, practice, late runs, and duplicate runs are labeled separately. Official direction correctness, the same-share zero-cost reference, and the costed account profit/loss are displayed separately and must not be added together. Maximum drawdown is based on recorded closing equity, not intraday equity.

Old Backtrader documents are saved-only, read-only history. They are not recalculated, merged into Zipline balances, or treated as new-engine evidence.

## Local data and privacy

Application data, configuration, logs, batches, databases, method registry, and accounts use the operating system's per-user application directories, outside the application bundle. Installing or replacing the application is not intended to replace those records. Raw evidence, model traffic, credentials, and daily results are not uploaded to GitHub; only a user-approved method package can be shared.

For application updates, see [Software updates](software-updates.md). For evaluation definitions and caveats, see [Paper evaluation](paper-evaluation.md).
