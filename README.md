# SHAQ Daily Oracle Lab

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
→ add an OpenAI, Anthropic or compatible API profile
→ enter the SEC research identity and verify the PIT universe/local storage
→ check team updates
→ select main and one or more Shadow versions
→ run one shared-evidence comparison
→ inspect the local dashboard
```

Python, Git, Codex, WorkBuddy and a Futu account are not required for research
mode. The internal test release provides three installers:

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
 six blind domain analyses → non-voting adversary → deterministic gate
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

Each Skill has a short `SKILL.md` and one `references/foundations.md`. A Shadow
version may change only these Markdown or reference files. The application
rejects Python, scripts, binaries, Actions, credentials, local paths and runtime
data before upload. `main` is read-only; each member publishes immutable versions
to `shadow/<github-login>` and every version stays bound to its original main
commit.

## Models

The same `ModelBackend` contract supports:

- OpenAI Responses API with native strict structured output, `store=false` and
  no tools;
- Anthropic Messages API with structured output and no tools;
- OpenAI Chat Completions-compatible endpoints, either native strict schema or
  local strict JSON Schema validation.

The app probes the exact endpoint, model, authentication style and output mode
before saving a profile. It does not silently switch a model or endpoint after a
401, 429, timeout or schema failure. Codex CLI is retained only as an advanced
Mac development backend and is not needed by the research application.

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
adversary result, deterministic gate and open-to-close results. File hashes and
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
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e ".[desktop,futu,test]"
python3 scripts/validate_release.py
python3 -m unittest discover -s tests -v
shaq-daily-oracle-desktop --smoke
```

The GitHub workflow builds and smoke-tests all three installers on native x64 or
arm64 runners. Installers contain no account identifier, email address, API key,
daily run record or development-machine path. Internal packages use SHA-256 files
and ad-hoc or temporary signing, so the operating system may ask the reviewer to
confirm the first launch.

No software license is granted in this internal review version.
