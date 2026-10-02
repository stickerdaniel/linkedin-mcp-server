"""The differential branches native cells do not reach, mapped to the exact
existing tests that model them.

Light on purpose: the accounting plugin reads it at collection, in every run,
to count those tests as each branch's model coverage (column ``unit``) when
they actually run, never the check that their names exist.
"""

from __future__ import annotations

#: Every R10 branch the native cells do not reach, with the exact existing
#: tests that model it: counted as model coverage when they run, never as
#: native. Keyed by branch; each names its row and its tests' node ids.
MODEL_COVERAGE: dict[str, tuple[str, tuple[str, ...]]] = {
    "queued busy": (
        "H-R10b",
        (
            "tests/test_daemon_liveness.py::TestAdmissionAndRetirementAreOneDecision"
            "::test_a_queued_call_counts_as_busy",
            "tests/test_daemon_liveness.py::TestTheControlRoutes"
            "::test_a_busy_owner_refuses_and_changes_nothing",
            "tests/test_cli_main.py::TestRetiringASharedBrowser"
            "::test_a_busy_owner_is_left_alone_and_nothing_changes",
        ),
    ),
    "lost reply": (
        "H-R10a-logout",
        (
            "tests/test_cli_main.py::TestRetiringASharedBrowser"
            "::test_a_lost_answer_is_never_reported_as_unsent",
        ),
    ),
    "post-send cancellation": (
        "H-R10a-logout",
        (
            "tests/test_cli_main.py::TestRetiringASharedBrowser"
            "::test_an_interrupt_after_sending_says_it_may_be_retiring",
            "tests/test_cli_main.py::TestRetiringASharedBrowser"
            "::test_an_interrupt_while_waiting_says_it_may_be_retiring",
            "tests/test_cli_main.py::TestRetiringASharedBrowser"
            "::test_an_interrupt_after_an_accepted_reply_says_it_may_be_retiring",
        ),
    ),
    "malformed success reply": (
        "H-R10a-logout",
        (
            "tests/test_cli_main.py::TestRetiringASharedBrowser"
            "::test_an_answer_this_build_does_not_recognise",
        ),
    ),
    "lease timeout": (
        "H-R10a-logout",
        (
            "tests/test_cli_main.py::TestRetiringASharedBrowser"
            "::test_a_profile_that_does_not_come_free_is_left_untouched",
            "tests/test_cli_main.py::TestRetiringASharedBrowser"
            "::test_an_import_whose_profile_stays_busy_is_refused_plainly",
        ),
    ),
    "successor race": (
        "H-R10a-logout",
        (
            "tests/test_cli_main.py::TestRetiringASharedBrowser"
            "::test_a_profile_that_does_not_come_free_is_left_untouched",
            "tests/test_daemon_liveness.py::TestTheControlRoutes"
            "::test_a_call_cannot_slip_in_between_the_verdict_and_the_retirement",
        ),
    ),
    "login success under an idle owner": (
        "H-R10a-login",
        (
            "tests/test_cli_main.py::TestRetiringASharedBrowser"
            "::test_login_starts_after_an_idle_owner_retires",
        ),
    ),
    "import success under an idle owner": (
        "H-R10a-login",
        (
            "tests/test_cli_main.py::TestRetiringASharedBrowser"
            "::test_import_waits_for_the_profile_after_an_idle_owner_retires",
        ),
    ),
}


#: Every R16 and R10a-login branch the native cells do not reach, with the
#: exact existing tests that model it: counted as model coverage when they
#: run, never as native. Each entry's first element names its row.
AUTH_MODEL_COVERAGE: dict[str, tuple[str, tuple[str, ...]]] = {
    "browser_open marker": (
        "H-R16-cold",
        (
            "tests/test_daemon_auth.py::TestTheFrontendActsOnTheMarker"
            "::test_no_login_starts_while_the_profile_may_still_be_held",
        ),
    ),
    "frontend wait expiring while the login continues": (
        "H-R16-failed",
        (
            "tests/test_daemon_auth.py::TestTheRepairRunsForReal"
            "::test_a_sign_in_slower_than_the_wait_gives_up_without_replaying",
        ),
    ),
    "non-replayable or mutating call repaired, never replayed": (
        "H-R16-cold",
        (
            "tests/test_daemon_auth.py::TestTheFrontendActsOnTheMarker"
            "::test_a_call_that_had_already_started_is_never_run_again",
            "tests/test_daemon_auth.py::TestTheRepairRunsForReal"
            "::test_a_tool_that_changes_something_is_never_replayed",
        ),
    ),
    "two frontends meeting one dead session": (
        "H-R16-second",
        (
            "tests/test_bootstrap.py::TestTwoClientsMeetingOneDeadSession"
            "::test_the_generation_stops_the_second_client",
        ),
    ),
    "import beside an idle owner": (
        "H-R10a-login",
        (
            "tests/test_cli_main.py::TestRetiringASharedBrowser"
            "::test_import_waits_for_the_profile_after_an_idle_owner_retires",
        ),
    ),
}


#: Every R12 branch and R14 path the native cells do not reach, with the
#: exact existing tests that model it: counted as model coverage when they
#: run, never as native. Each entry's first element names its row.
ELIGIBILITY_MODEL_COVERAGE: dict[str, tuple[str, tuple[str, ...]]] = {
    "refused storage: no descriptor read, no transient lock, no state": (
        "H-R12-synced",
        (
            "tests/test_daemon.py::TestStorageEligibility"
            "::test_an_ineligible_root_keeps_direct_with_one_warning",
            "tests/test_cli_main.py::TestForwardingToASharedOwner"
            "::test_no_owner_is_sought_on_storage_that_is_not_local",
        ),
    ),
    "non-local mounts (no rootless fixture on a hosted runner)": (
        "H-R12-unknown",
        (
            "tests/test_storage_class.py::TestLinuxMounts"
            "::test_every_nfs_and_smb_variant_is_nonlocal",
            "tests/test_storage_class.py::TestMacFilesystems"
            "::test_a_volume_without_mnt_local_is_nonlocal",
            "tests/test_storage_class.py::TestWindowsVolumes"
            "::test_drive_type_then_filesystem",
        ),
    ),
    "a classifier or state root that cannot be read": (
        "H-R12-unknown",
        (
            "tests/test_daemon.py::TestStorageEligibility"
            "::test_a_classifier_that_raises_keeps_direct",
            "tests/test_daemon.py::TestStorageEligibility"
            "::test_an_unlocatable_state_root_keeps_direct",
            "tests/test_daemon.py::TestStorageEligibility"
            "::test_a_reader_failure_in_the_real_classifier_keeps_direct",
        ),
    ),
    "other providers: OneDrive, Windows Dropbox locations, iCloud Drive": (
        "H-R12-synced",
        (
            "tests/test_storage_class.py::TestWindowsProviders"
            "::test_a_onedrive_variable_names_a_synced_root",
            "tests/test_storage_class.py::TestWindowsProviders"
            "::test_dropbox_is_read_from_both_windows_locations",
            "tests/test_storage_class.py::TestClassification"
            "::test_mac_provider_folders_are_synced",
        ),
    ),
    "disabled: no election sought": (
        "H-R12-disabled-env",
        (
            "tests/test_cli_main.py::TestForwardingToASharedOwner"
            "::test_no_owner_is_sought_when_the_daemon_is_switched_off",
            "tests/test_config.py::TestLoaders::test_no_daemon_flag_overrides_env_true",
        ),
    ),
    "HTTP: no election, on every platform": (
        "H-R12-http",
        (
            "tests/test_daemon.py::TestWhetherTheDaemonAppliesAtAll"
            "::test_an_explicit_http_bind_does_not_use_a_daemon",
            "tests/test_cli_main.py::TestForwardingToASharedOwner"
            "::test_no_owner_is_sought_for_an_http_server",
            "tests/test_cli_main.py::TestForwardingToASharedOwner"
            "::test_an_interactively_chosen_http_transport_elects_no_daemon",
            "tests/test_daemon.py::TestWhetherTheDaemonAppliesAtAll"
            "::test_container_http_needs_no_daemon_warning",
        ),
    ),
    "container: refused with its warning": (
        "H-R12-container",
        (
            "tests/test_daemon.py::TestWhetherTheDaemonAppliesAtAll"
            "::test_a_container_refuses_the_daemon_and_says_why",
        ),
    ),
    "rival answered: left alone, no turnover, no lock": (
        "H-R14",
        (
            "tests/test_daemon_election.py::TestAnOwnerThisBuildMayOnlyControl"
            "::test_a_live_owner_of_this_build_with_another_configuration_is_left_alone",
        ),
    ),
    "rival silent: one probe, then Direct": (
        "H-R14",
        (
            "tests/test_daemon_election.py::TestAnOwnerThisBuildMayOnlyControl"
            "::test_a_silent_owner_of_this_build_with_another_configuration_is_left_alone",
        ),
    ),
    "rival refused: leftovers buried, the election goes on": (
        "H-R14",
        (
            "tests/test_daemon_election.py::TestAnOwnerThisBuildMayOnlyControl"
            "::test_a_dead_owner_of_another_configuration_is_leftovers",
        ),
    ),
    "another build's configuration: not probed": (
        "H-R14",
        (
            "tests/test_daemon_election.py::TestAnOwnerThisBuildMayOnlyControl"
            "::test_another_builds_configuration_is_not_probed",
        ),
    ),
    "a configuration mismatch is read, and its pair is control only": (
        "H-R14",
        (
            "tests/test_daemon.py::TestRefusing"
            "::test_a_daemon_with_a_different_configuration_is_refused",
            "tests/test_daemon_proxy.py::TestControlOnlyNeverRunsATool"
            "::test_a_backend_cannot_be_built_around_one",
            "tests/test_daemon_proxy.py::TestControlOnlyNeverRunsATool"
            "::test_a_call_is_not_preflighted_or_sent_to_one",
        ),
    ),
    "the minimum hold is not fingerprinted": (
        "H-R14",
        (
            "tests/test_daemon_descriptor.py::TestConfigFingerprint"
            "::test_configuration_that_only_affects_the_client_still_matches",
        ),
    ),
}


#: Every mapping the accounting counts.
COUNTED = {**MODEL_COVERAGE, **AUTH_MODEL_COVERAGE, **ELIGIBILITY_MODEL_COVERAGE}


def model_rows() -> dict[str, list[str]]:
    """Each mapped test's node id, without parameters, and the rows it models."""
    found: dict[str, list[str]] = {}
    for row, nodes in COUNTED.values():
        for node in nodes:
            rows = found.setdefault(node, [])
            if row not in rows:
                rows.append(row)
    return found
