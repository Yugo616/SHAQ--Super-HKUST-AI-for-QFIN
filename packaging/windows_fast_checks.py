"""Dependency-light reliability gate; also runnable on other hosts for import checks."""
from pathlib import Path
import sys
import unittest


TESTS = (
    'test_public_base_update_acceptance',
    'test_update_smoke.InstalledAcceptanceContractTests.test_windows_restart_confirmation_survives_exclusive_receipt_publish_window',
    'test_update_smoke.InstalledAcceptanceContractTests.test_restart_confirmation_retries_only_bounded_windows_read_denial',
    'test_model_recovery_fixes.DeadlineTests',
    'test_model_recovery.ExecutionRecoveryTests.test_execution_settings_change_without_changing_saved_model',
    'test_model_recovery.ExecutionRecoveryTests.test_windows_tree_job_is_owned_and_windowless',
    'test_model_recovery.ExecutionRecoveryTests.test_timeout_retries_once_and_policy_does_not_change_cache_identity',
    'test_model_recovery.ExecutionRecoveryTests.test_authentication_and_schema_errors_are_not_retried',
    'test_model_recovery.ExecutionRecoveryTests.test_timeout_terminates_only_own_process_tree',
    'test_model_recovery.ExecutionRecoveryTests.test_compact_tables_roundtrip_missing_null_and_all_ohlc',
    'test_model_compatibility',
    'test_cli_discovery',
    'test_research_schedule',
    'test_collection_failure_state.CollectionFailureStateTests.test_child_collection_error_is_persisted_and_owned_job_lock_released',
    'test_collection_failure_state.CollectionFailureStateTests.test_initial_persistence_failure_still_terminates_in_memory_and_releases_lock',
    'test_collection_worker.CollectionWorkerTests.test_parent_owns_scratch_cleanup_even_when_child_crashes',
    'test_collection_worker.CollectionWorkerTests.test_worker_failures_are_sanitized_and_never_empty_success',
    'test_collection_worker.CollectionWorkerTests.test_worker_failure_diagnostic_preserves_resource_reason_not_secrets',
    'test_collection_worker.CollectionWorkerTests.test_frozen_entry_dispatches_worker_before_gui_or_updater',
    'test_collection_worker.CollectionWorkerTests.test_source_ping_and_windowless_entry_roundtrip_have_no_provider_side_effects',
    # The real shared-fixture receipt integration also installs bundled methods
    # and requires QuickJS. Keep it in full discovery, not this lightweight gate.
    'test_desktop_lifecycle.DesktopLifecycleTests.test_api_retains_bound_method_discovery_and_rpc_parameter_names',
    'test_desktop_lifecycle.DesktopLifecycleTests.test_disposable_api_drains_admitted_writer_and_rejects_late_requests',
    'test_desktop_lifecycle.DesktopLifecycleTests.test_request_exception_releases_lifetime_without_exposing_private_methods',
    'test_worker_protocol_acceptance',
)


def main():
    root = Path(__file__).resolve().parents[1]
    # Some existing test fixtures intentionally use sibling imports. Keep the
    # same import topology as unittest discover -s tests, including child env.
    sys.path[:0] = [str(root / 'tests'), str(root / 'src')]
    import os
    os.environ['PYTHONPATH'] = os.pathsep.join((str(root / 'src'), str(root / 'tests')))
    suite = unittest.defaultTestLoader.loadTestsFromNames(TESTS)
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
