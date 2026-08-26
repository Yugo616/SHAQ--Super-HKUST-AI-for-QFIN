from __future__ import annotations

import csv
import json
import os
import tempfile
import time
from datetime import date, datetime, time as clock_time
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .events import _cik_number, _fetch, _sec_identity
from .hashing import sha256_file, sha256_payload
from .postmortem import (
    PostmortemError,
    build_outcome_document,
    build_postmortem,
    validate_outcome_document,
    validate_postmortem,
)
from .reports import write_reports
from .sandboxed_codex import _codex_call, _load_config
from .sec_view import build_sec_analysis_text
from .workflow import _resolve_universe


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_immutable(path: Path, value: dict[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(f"immutable postmortem artifact exists: {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.{os.getpid()}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _postmortem_schema() -> dict[str, Any]:
    hypothesis = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "hypothesis_id", "symbol", "diagnostic_category", "economic_mechanism",
            "affected_domains", "expected_improvement", "invalidation_conditions",
            "reference_ids", "source_ids", "alternative_explanations",
            "strongest_countercase",
        ],
        "properties": {
            "hypothesis_id": {"type": "string", "minLength": 1},
            "symbol": {"type": "string", "minLength": 1},
            "diagnostic_category": {
                "type": "string",
                "enum": [
                    "evidence_missing", "information_already_absorbed", "horizon_mismatch",
                    "gate_overcautious", "post_cutoff_shock", "evidence_interpretation",
                    "unexplained",
                ],
            },
            "economic_mechanism": {"type": "string", "minLength": 1},
            "affected_domains": {
                "type": "array", "uniqueItems": True,
                "items": {
                    "type": "string",
                    "enum": [
                        "market", "relationships", "event", "capital",
                        "derivatives", "price_volume",
                    ],
                },
            },
            "expected_improvement": {"type": "string", "minLength": 1},
            "invalidation_conditions": {
                "type": "array", "items": {"type": "string", "minLength": 1}
            },
            "reference_ids": {
                "type": "array", "uniqueItems": True,
                "items": {"type": "string", "minLength": 1},
            },
            "source_ids": {
                "type": "array", "uniqueItems": True,
                "items": {"type": "string", "minLength": 1},
            },
            "alternative_explanations": {
                "type": "array", "items": {"type": "string", "minLength": 1}
            },
            "strongest_countercase": {"type": "string", "minLength": 1},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["hypotheses"],
        "properties": {
            "hypotheses": {"type": "array", "maxItems": 20, "items": hypothesis}
        },
    }


class PostmortemRunner:
    def __init__(
        self, *, package_root: Path, runtime_root: Path, host: str = "127.0.0.1",
        port: int = 11111, now: Callable[[], datetime] | None = None,
    ) -> None:
        self.package_root = package_root.resolve()
        self.runtime_root = runtime_root.resolve()
        self.host = host
        self.port = port
        self.zone = ZoneInfo("America/New_York")
        self.now = now or (lambda: datetime.now(self.zone))
        self.config = _read(self.package_root / "config/postmortem.json")
        self._validate_config()

    def _validate_config(self) -> None:
        bindings = self.config.get("parameter_bindings", {})
        for name in (
            "provisional_capture_after_et", "final_reobservation_after_et",
            "market_symbol", "batch_review_interval_trading_days",
            "first_batch_review_date", "batch_review_after_et", "drift_alert_delta",
            "automatic_production_mutation_allowed", "approved_reference_ids",
        ):
            if len(bindings.get(name, [])) != 3:
                raise PostmortemError(f"postmortem config lacks governed {name}")
        if self.config.get("automatic_production_mutation_allowed") is not False:
            raise PostmortemError("postmortem cannot mutate production")
        if int(self.config["batch_review_interval_trading_days"]) <= 0:
            raise PostmortemError("batch review interval must be positive")
        if not 0 < float(self.config["drift_alert_delta"]) < 1:
            raise PostmortemError("drift delta must be between zero and one")

    def _runtime(self, session_date: date) -> Path:
        matches = sorted(self.runtime_root.glob(f"SHAQ-CANARY-{session_date.isoformat()}-*"))
        complete = [path for path in matches if (path / "frozen_run.json").is_file()]
        if not complete:
            raise PostmortemError("session has no frozen run to review")
        return complete[-1]

    def _timing(self, *, session_date: date, phase: str) -> None:
        observed = self.now()
        if observed.tzinfo is None or observed.utcoffset() is None:
            raise PostmortemError("postmortem clock requires an offset")
        if phase == "provisional":
            threshold = datetime.combine(
                session_date,
                clock_time.fromisoformat(self.config["provisional_capture_after_et"]),
                self.zone,
            )
            if observed.date() != session_date or observed < threshold:
                raise PostmortemError("provisional postmortem is only allowed after the same session closes")
        elif phase == "final":
            threshold = datetime.combine(
                session_date,
                clock_time.fromisoformat(self.config["final_reobservation_after_et"]),
                self.zone,
            )
            if observed.date() <= session_date or observed.time() < threshold.time():
                raise PostmortemError("final postmortem requires a later-date reobservation")
        else:
            raise PostmortemError("postmortem phase must be provisional or final")

    def _symbols(self, frozen: dict[str, Any], intake: dict[str, Any]) -> list[str]:
        candidates = {str(row["symbol"]).upper() for row in intake.get("candidates", [])}
        if candidates != set(frozen.get("reports_by_symbol", {})):
            raise PostmortemError("candidate intake differs from frozen reports")
        sectors = {
            str(row.get("sector_benchmark", "")).upper()
            for row in intake.get("candidates", [])
        }
        sectors.discard("")
        return sorted(candidates | sectors | {str(self.config["market_symbol"]).upper()})

    def _capture_outcomes(
        self, *, frozen: dict[str, Any], intake: dict[str, Any], trade_date: date, phase: str,
    ) -> dict[str, Any]:
        from futu import AuType, KLType, OpenQuoteContext, RET_OK, Session  # type: ignore

        quote = OpenQuoteContext(host=self.host, port=self.port)
        rows_by_symbol: dict[str, list[dict[str, Any]]] = {}
        try:
            for symbol in self._symbols(frozen, intake):
                ret, frame, next_key = quote.request_history_kline(
                    f"US.{symbol.replace('-', '.')}", start=trade_date.isoformat(),
                    end=trade_date.isoformat(), ktype=KLType.K_DAY, autype=AuType.NONE,
                    max_count=10, session=Session.RTH,
                )
                if ret != RET_OK or next_key is not None:
                    raise PostmortemError(f"unadjusted RTH outcome query failed for {symbol}")
                rows_by_symbol[symbol] = [row.to_dict() for _, row in frame.iterrows()]
        finally:
            quote.close()
        captured = self.now()
        return build_outcome_document(
            run_id=frozen["run_id"], frozen_run_sha256=frozen["run_sha256"],
            trade_date=trade_date.isoformat(), phase=phase,
            captured_at_et=captured.isoformat(),
            session_close_et=datetime.combine(
                trade_date, clock_time(16, 0), self.zone
            ).isoformat(),
            rows_by_symbol=rows_by_symbol,
        )

    def _relationships(
        self, *, runtime: Path, intake: dict[str, Any]
    ) -> dict[str, dict[str, Any]]:
        manifest = _read(runtime / "evidence_manifest.json")
        records = {row["evidence_id"]: row for row in manifest.get("evidence", [])}
        root = runtime / "evidence"
        output = {}
        for candidate in intake.get("candidates", []):
            symbol = str(candidate["symbol"]).upper()
            evidence_id = f"futu-relationships-{symbol.lower()}"
            record = records.get(evidence_id)
            if not record:
                raise PostmortemError(f"{symbol} frozen relationship evidence is missing")
            path = (root / record["raw_file_path"]).resolve()
            try:
                path.relative_to(root.resolve())
            except ValueError as exc:
                raise PostmortemError("relationship evidence escapes the frozen root") from exc
            if not path.is_file() or sha256_file(path) != record.get("raw_sha256"):
                raise PostmortemError("frozen relationship evidence hash mismatch")
            output[symbol] = _read(path)
        return output

    def _capture_post_cutoff_sec_sources(
        self, *, runtime: Path, frozen: dict[str, Any], intake: dict[str, Any],
        trade_date: date, post_root: Path,
    ) -> dict[str, Any]:
        manifest_path = post_root / "post_cutoff_sources.json"
        if manifest_path.exists():
            return _read(manifest_path)
        universe = _resolve_universe(self.runtime_root)
        with universe.open(newline="", encoding="utf-8-sig") as handle:
            members = {
                str(row.get("ticker", row.get("instrument", ""))).upper(): row
                for row in csv.DictReader(handle)
            }
        user_agent = _sec_identity()
        cutoff = datetime.fromisoformat(frozen["cutoff_et"].replace("Z", "+00:00"))
        session_close = datetime.combine(trade_date, clock_time(16, 0), self.zone)
        analysis_config = _read(self.package_root / "config/event-analysis.json")
        sources = []
        errors = []
        source_root = post_root / "sources"
        for candidate in intake.get("candidates", []):
            symbol = str(candidate["symbol"]).upper()
            try:
                cik = _cik_number(str(members.get(symbol, {}).get("cik_company_id", "")))
                submissions_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
                submissions = json.loads(_fetch(submissions_url, user_agent))
                recent = submissions.get("filings", {}).get("recent", {})
                fields = ("accessionNumber", "acceptanceDateTime", "form", "primaryDocument")
                if any(not isinstance(recent.get(field), list) for field in fields):
                    raise PostmortemError("SEC recent-filings schema is incomplete")
                for accession, accepted_text, form, primary in zip(
                    *(recent[field] for field in fields), strict=False
                ):
                    if str(form) not in {"8-K", "10-Q", "10-K", "6-K", "20-F"}:
                        continue
                    accepted = datetime.fromisoformat(str(accepted_text).replace("Z", "+00:00"))
                    if accepted.tzinfo is None:
                        accepted = accepted.replace(tzinfo=self.zone)
                    if not cutoff < accepted <= session_close:
                        continue
                    compact = str(accession).replace("-", "")
                    uri = (
                        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                        f"{compact}/{primary}"
                    )
                    raw = _fetch(uri, user_agent)
                    source_root.mkdir(parents=True, exist_ok=True)
                    raw_path = source_root / f"{symbol}-{compact}.bin"
                    analysis_path = source_root / f"{symbol}-{compact}.analysis.txt"
                    if raw_path.exists() or analysis_path.exists():
                        raise FileExistsError("post-cutoff SEC source is partially present")
                    raw_path.write_bytes(raw)
                    analysis_path.write_bytes(build_sec_analysis_text(
                        raw, document_types=analysis_config["document_types"],
                        maximum_output_bytes=int(analysis_config["maximum_output_bytes"]),
                    ))
                    sources.append({
                        "source_id": f"sec:{accession}",
                        "symbol": symbol,
                        "form": str(form),
                        "published_at_et": accepted.isoformat(),
                        "source_uri": uri,
                        "raw_sha256": sha256_file(raw_path),
                        "analysis_sha256": sha256_file(analysis_path),
                        "analysis_file": str(analysis_path.relative_to(post_root)),
                    })
                time.sleep(0.11)
            except Exception as exc:
                errors.append({
                    "symbol": symbol,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                })
        unsigned = {
            "schema_version": 1,
            "run_id": frozen["run_id"],
            "frozen_run_sha256": frozen["run_sha256"],
            "window_start_et": cutoff.isoformat(),
            "window_end_et": session_close.isoformat(),
            "sources": sorted(sources, key=lambda row: row["source_id"]),
            "errors": sorted(errors, key=lambda row: row["symbol"]),
        }
        value = {**unsigned, "source_manifest_sha256": sha256_payload(unsigned)}
        _write_immutable(manifest_path, value)
        return value

    def _ai_hypotheses(
        self, *, post_root: Path, deterministic: dict[str, Any], source_manifest: dict[str, Any],
    ) -> list[dict[str, Any]]:
        call_path = post_root / "ai_calls" / "provisional.json"
        prompt_path = post_root / "ai_calls" / "provisional.prompt"
        packet_path = post_root / "ai_calls" / "provisional.packet.json"
        if call_path.exists():
            saved = _read(call_path)
            if not prompt_path.is_file() or not packet_path.is_file():
                raise PostmortemError("postmortem AI call is partially present")
            return saved.get("raw_output", {}).get("hypotheses", [])
        registry = _read(self.package_root / "governance/postmortem-registry.json")
        source_packet = []
        for source in source_manifest.get("sources", []):
            analysis_path = post_root / source["analysis_file"]
            if not analysis_path.is_file() or sha256_file(analysis_path) != source["analysis_sha256"]:
                raise PostmortemError("post-cutoff analysis source hash mismatch")
            source_packet.append({
                **{key: value for key, value in source.items() if key != "analysis_file"},
                "deterministic_analysis_text": analysis_path.read_text(
                    encoding="utf-8", errors="replace"
                ),
            })
        packet = {
            "run_id": deterministic["run_id"],
            "trade_date": deterministic["trade_date"],
            "policy": {
                "causal_claim": "hypothesis_not_fact",
                "automatic_mutation_allowed": False,
                "single_day_performance_change_allowed": False,
            },
            "approved_references": registry["references"],
            "candidate_diagnostics": deterministic["candidate_diagnostics"],
            "post_cutoff_primary_sources": source_packet,
        }
        prompt = (
            "You are the isolated retrospective reviewer for SHAQ Daily Oracle. This is not a "
            "prediction and not training. Use only the JSON packet. Produce zero or more concise "
            "research hypotheses; never claim a unique true cause. A hypothesis must cite only the "
            "supplied reference IDs and source IDs. Timing alone does not prove causality. Use "
            "post_cutoff_shock only when a captured post-cutoff primary source supports it. Do not "
            "change, recommend bypassing, or assign a score to the production gate. Every proposal "
            "must include a falsifiable invalidation condition and its strongest countercase. If the "
            "packet does not support a useful mechanism, return an empty list.\n\n"
            f"PACKET JSON:\n{json.dumps(packet, ensure_ascii=False, sort_keys=True)}"
        )
        config = _load_config(_read(self.package_root / "config/ai-backend.json"))
        parsed, audit = _codex_call(
            prompt=prompt, schema=_postmortem_schema(), config=config,
            workspace_root=self.package_root.parent,
        )
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt, encoding="utf-8")
        packet_path.write_text(
            json.dumps(packet, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        _write_immutable(call_path, {
            "schema_version": 1,
            "input_sha256": sha256_payload(packet),
            "prompt_sha256": sha256_file(prompt_path),
            "packet_sha256": sha256_file(packet_path),
            "audit": audit,
            "raw_output": parsed,
        })
        return parsed.get("hypotheses", [])

    def run(self, *, session_date: date, phase: str) -> Path:
        self._timing(session_date=session_date, phase=phase)
        runtime = self._runtime(session_date)
        frozen = _read(runtime / "frozen_run.json")
        intake = _read(runtime / "candidate_intake.json")
        post_root = runtime / "postmortem"
        outcomes_path = post_root / f"outcomes_{phase}.json"
        result_path = post_root / f"postmortem_{phase}.json"
        if result_path.exists():
            if not outcomes_path.is_file():
                raise PostmortemError("postmortem result exists without its outcome observation")
            validate_outcome_document(_read(outcomes_path), frozen)
            validate_postmortem(_read(result_path), frozen)
            return runtime
        if outcomes_path.exists():
            outcomes = validate_outcome_document(_read(outcomes_path), frozen)
        else:
            outcomes = self._capture_outcomes(
                frozen=frozen, intake=intake, trade_date=session_date, phase=phase
            )
            _write_immutable(outcomes_path, outcomes)
        relationships = self._relationships(runtime=runtime, intake=intake)
        if phase == "provisional":
            source_manifest = self._capture_post_cutoff_sec_sources(
                runtime=runtime, frozen=frozen, intake=intake,
                trade_date=session_date, post_root=post_root,
            )
            sources = source_manifest.get("sources", [])
            base = build_postmortem(
                frozen=frozen, candidate_intake=intake,
                relationships_by_symbol=relationships, outcomes=outcomes,
                generated_at_et=self.now().isoformat(),
                approved_reference_ids=set(self.config["approved_reference_ids"]),
                post_cutoff_sources=sources,
            )
            hypotheses: list[dict[str, Any]] = []
            ai_failure_path = post_root / "ai_failure.json"
            if (
                os.environ.get("DAILY_ORACLE_DISABLE_POSTMORTEM_AI") != "1"
                and not ai_failure_path.exists()
            ):
                try:
                    hypotheses = self._ai_hypotheses(
                        post_root=post_root, deterministic=base,
                        source_manifest=source_manifest,
                    )
                except Exception as exc:
                    _write_immutable(ai_failure_path, {
                        "schema_version": 1,
                        "recorded_at_et": self.now().isoformat(),
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "effect": "deterministic_postmortem_continues_without_AI_hypotheses",
                    })
            result = build_postmortem(
                frozen=frozen, candidate_intake=intake,
                relationships_by_symbol=relationships, outcomes=outcomes,
                generated_at_et=self.now().isoformat(),
                approved_reference_ids=set(self.config["approved_reference_ids"]),
                post_cutoff_sources=sources, ai_hypotheses=hypotheses,
            )
        else:
            provisional_path = post_root / "postmortem_provisional.json"
            source_path = post_root / "post_cutoff_sources.json"
            provisional_outcomes_path = post_root / "outcomes_provisional.json"
            if not all(path.is_file() for path in (
                provisional_path, source_path, provisional_outcomes_path
            )):
                raise PostmortemError("finalization requires the immutable provisional review")
            provisional = validate_postmortem(_read(provisional_path), frozen)
            source_manifest = _read(source_path)
            result = build_postmortem(
                frozen=frozen, candidate_intake=intake,
                relationships_by_symbol=relationships, outcomes=outcomes,
                generated_at_et=self.now().isoformat(),
                approved_reference_ids=set(self.config["approved_reference_ids"]),
                post_cutoff_sources=source_manifest.get("sources", []),
                ai_hypotheses=[{
                    key: value for key, value in row.items()
                    if key not in {
                        "status", "causal_claim_policy", "automatic_mutation_allowed"
                    }
                } for row in provisional.get("learning_hypotheses", [])],
            )
            prior = _read(provisional_outcomes_path)
            revisions = []
            for symbol, row in outcomes["rows_by_symbol"].items():
                previous = prior["rows_by_symbol"].get(symbol, {})
                if any(row.get(field) != previous.get(field) for field in (
                    "official_open", "official_high", "official_low",
                    "official_close", "official_volume",
                )):
                    revisions.append(symbol)
            comparison = {
                "schema_version": 1,
                "run_id": frozen["run_id"],
                "provisional_outcome_sha256": prior["outcome_document_sha256"],
                "final_outcome_sha256": outcomes["outcome_document_sha256"],
                "revised_symbols": sorted(revisions),
                "final_is_independent_reobservation": True,
            }
            comparison["comparison_sha256"] = sha256_payload(comparison)
            _write_immutable(post_root / "reobservation_comparison.json", comparison)
        _write_immutable(result_path, result)
        _write_immutable(post_root / f"manifest_{phase}.json", {
            "schema_version": 1,
            "run_id": frozen["run_id"],
            "phase": phase,
            "frozen_run_sha256": frozen["run_sha256"],
            "outcomes_sha256": sha256_file(outcomes_path),
            "postmortem_sha256": sha256_file(result_path),
        })
        availability = runtime / "domain_availability_status.json"
        journal = runtime / "broker_journal.json"
        labels = runtime / "labels_provisional.json"
        write_reports(
            runtime=runtime,
            frozen=frozen,
            collection_statuses=_read(availability) if availability.is_file() else None,
            orders=_read(journal) if journal.is_file() else None,
            labels=_read(labels) if labels.is_file() else None,
            postmortem=result,
        )
        return runtime
