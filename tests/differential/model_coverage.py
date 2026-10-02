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
        "H-R10a-logout",
        (
            "tests/test_cli_main.py::TestRetiringASharedBrowser"
            "::test_login_starts_after_an_idle_owner_retires",
        ),
    ),
    "import success under an idle owner": (
        "H-R10a-logout",
        (
            "tests/test_cli_main.py::TestRetiringASharedBrowser"
            "::test_import_waits_for_the_profile_after_an_idle_owner_retires",
        ),
    ),
}


def model_rows() -> dict[str, list[str]]:
    """Each mapped test's node id, without parameters, and the rows it models."""
    found: dict[str, list[str]] = {}
    for row, nodes in MODEL_COVERAGE.values():
        for node in nodes:
            rows = found.setdefault(node, [])
            if row not in rows:
                rows.append(row)
    return found
