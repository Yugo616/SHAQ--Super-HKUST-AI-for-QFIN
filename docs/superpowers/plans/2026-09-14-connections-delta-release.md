# SHAQ Lab connections and delta release

User-approved scope: three model entry choices, diagnostic API errors, regression acceptance of existing balance/version/replay features, real Velopack update integration and three-platform releases. No prediction, method package, sizing, fee, label, or historical-fill changes. No runtime/credentials in public source.

## Task 1: Model connection UI and errors

Implement Codex / Claude / API peer choices in first setup and settings. API reveals official/relay form without requiring local CLI. Preserve unsaved per-provider inputs; never forward a relay key to an official provider on switching. Only a successful real probe replaces a working saved connection. Existing valid provider configuration must survive failed probes.

Use existing model backend and desktop handlers. Provide structured, bounded, redacted HTTP status/provider code/provider message/request ID errors in Chinese UI with edit/retry/copy. Do not send unnecessary sampling parameters, rewrite Packy domains, silently change providers/models/output standards, or include raw keys in diagnostic objects. Native schema and locally validated JSON remain distinct.

TDD tests: three entry choices and draft restoration through actual JS handlers; provider switching clears active key until that provider draft restored; failed connection leaves saved profile and secret intact; HTTP 400/401/403/429 diagnostics, malformed/non-JSON body, timeout, token redaction in message and headers, request IDs. Regression existing CLI UTF8/Windows paths. Do not change updater files or account logic.

## Task 2: Velopack runtime, packaging and update acceptance

Integrate supported pinned Python SDK and matching vpk CLI. Early lifecycle hook, GUI check/download/apply-restart, platform feeds from configured repository, fail closed against wrong/corrupt packages, no application replacement during any active analysis/settlement/background write. User data external to install. Bridge/full fallback explicit. Real app V1-to-V2 installed tests on all three platforms; no fixture-only evidence claimed as full acceptance. Two release pages, architecture-separated machine feeds. Do not publish before verified.

## Task 3: Existing feature acceptance and documentation

Verify account continuity/canonical aliases/volatility sizing unchanged, missing-minute UI not zero trade, preliminary/rechecked/revised labels, replay selection stability, progress isolation, comparison identity distinctions, GUI labels and auto-run editing. Patch only actual gaps using TDD. Human README Chinese/English with truthful installation/update/connection instructions. The user declined publication of private replay screenshots; do not capture or publish them.

### Approved addition: two method-transfer dialogs

Keep the editor and its save action. The top toolbar offers Download and Upload, each opening an in-app multiselect dialog. Download discovers the configured versions catalog, displays names/authors/version metadata, and disables already-installed equivalent content. Upload lists saved locally-owned methods absent from the remote catalog, with explicit confirmation and per-item results. Opening either dialog does not publish anything.

Use a deterministic full-method content digest for transfer deduplication, separate from existing manifest, run, and account identities. Unrelated branch commits or display-name changes do not require a repeated download/upload. Verify content, preserve immutable versions and provenance, pin selected remote snapshots, and recheck concurrent uploads. Unresolvable legacy baselines must not be treated as equal to current main. Do not rewrite historical accounts or existing method packages. Cover batch transfer, partial failure, no-write-before-confirmation, duplicate content, changed content, concurrency, read-only accounts, and preservation of editor state.

## Task 4: Release gate

Full existing suite and eight Skills. Windows Unicode username/spaces/native CLI; real model vs fixtures separated. Three actual installed update tests with restart/state retention/corruption/network/busy checks. Source/dependency/privacy audits. Same commit all artifacts, no overwritten releases, versions branch unchanged. Publish only accepted new artifacts and report anything unverified. Local installed app update only after active runs end.
