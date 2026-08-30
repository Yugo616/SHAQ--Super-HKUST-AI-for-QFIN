"""Deterministic safety core for SHAQ Daily Oracle."""

from .canary import CanaryError, build_canary_intents
from .candidates import CandidateError, select_candidates
from .capture import (
    CaptureError,
    build_capture_receipt,
    extract_sec_acceptance_proof,
    validate_public_https_url,
    verify_publication_proof,
)
from .audit import AuditError, audit_runtime
from .contracts import ContractError, validate_adversary_report, validate_domain_report
from .collectors import (
    COLLECTION_STATUSES,
    CollectorError,
    build_capital_analysis,
    build_capital_document,
    build_derivatives_document,
    build_relationship_document,
    collection_status,
)
from .execution import (
    ExecutionError,
    apply_broker_update,
    broker_remark,
    broker_update_from_row,
    enforce_execution_window,
    exit_quantity_from_entry,
    find_broker_order,
    normalize_futu_order_status,
    reconciled_journal_status,
    phase_is_terminal,
    verify_execution_bundle,
    register_intent,
    select_simulate_us_account,
)
from .fallback import build_no_ai_run_input
from .evidence_manifest import (
    ManifestError,
    build_primary_event_record,
    build_snapshot_evidence_manifest,
    merge_evidence_manifests,
)
from .ledger import LedgerError, evaluation_record, execution_cost_components
from .labels import (
    LabelError,
    build_label_row,
    validate_label_capture_time,
    validate_label_document,
)
from .isolation import IsolationError, formal_ai_status, validate_isolation_status
from .sandboxed_codex import (
    SandboxedCodexError,
    attest_openai_responses,
    attest_sandboxed_codex,
    build_seatbelt_profile,
    derive_predictions,
    run_sandboxed_six_domain,
    run_six_domain,
    verify_isolation_attestation,
)
from .lineage import EvidenceError, build_lineage_graph
from .premarket import (
    PremarketSemanticError,
    build_symbol_snapshot_documents,
    resolve_premarket_return,
)
from .price_history import (
    PriceHistoryError,
    build_price_history_analysis,
    build_price_history_document,
)
from .readiness import (
    ReadinessError,
    build_prospective_evaluations,
    cost_model,
    net_profit_readiness,
    probability_readiness,
)
from .run import RunError, freeze_run
from .sec_view import (
    SecViewError,
    build_sec_analysis_text,
    build_sec_view_receipt,
    sec_document_types,
    verify_sec_view_receipt,
)
from .tasks import TaskError, build_blind_domain_tasks
from .universe import UniverseError, derive_effective_universe

__all__ = [
    "CanaryError",
    "CandidateError",
    "CaptureError",
    "AuditError",
    "ContractError",
    "CollectorError",
    "EvidenceError",
    "ExecutionError",
    "LedgerError",
    "ManifestError",
    "LabelError",
    "IsolationError",
    "SandboxedCodexError",
    "ReadinessError",
    "PremarketSemanticError",
    "PriceHistoryError",
    "RunError",
    "SecViewError",
    "TaskError",
    "UniverseError",
    "apply_broker_update",
    "COLLECTION_STATUSES",
    "audit_runtime",
    "broker_update_from_row",
    "broker_remark",
    "enforce_execution_window",
    "build_canary_intents",
    "build_capital_document",
    "build_capital_analysis",
    "build_derivatives_document",
    "build_relationship_document",
    "collection_status",
    "build_no_ai_run_input",
    "build_capture_receipt",
    "extract_sec_acceptance_proof",
    "derive_effective_universe",
    "build_blind_domain_tasks",
    "build_lineage_graph",
    "build_symbol_snapshot_documents",
    "build_snapshot_evidence_manifest",
    "build_primary_event_record",
    "build_price_history_document",
    "build_price_history_analysis",
    "build_prospective_evaluations",
    "build_label_row",
    "cost_model",
    "net_profit_readiness",
    "evaluation_record",
    "execution_cost_components",
    "exit_quantity_from_entry",
    "find_broker_order",
    "formal_ai_status",
    "attest_sandboxed_codex",
    "attest_openai_responses",
    "build_seatbelt_profile",
    "build_sec_analysis_text",
    "build_sec_view_receipt",
    "sec_document_types",
    "derive_predictions",
    "run_sandboxed_six_domain",
    "run_six_domain",
    "verify_isolation_attestation",
    "freeze_run",
    "normalize_futu_order_status",
    "phase_is_terminal",
    "merge_evidence_manifests",
    "reconciled_journal_status",
    "verify_execution_bundle",
    "register_intent",
    "select_simulate_us_account",
    "select_candidates",
    "probability_readiness",
    "resolve_premarket_return",
    "validate_adversary_report",
    "validate_label_capture_time",
    "validate_label_document",
    "validate_isolation_status",
    "validate_domain_report",
    "validate_public_https_url",
    "verify_publication_proof",
    "verify_sec_view_receipt",
]
