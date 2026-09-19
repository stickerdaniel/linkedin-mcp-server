"""Collection contracts for the daemon election stress."""

import builtins
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_TEST_MODULE = Path(__file__).with_name("test_daemon_election.py")
_TARGET = "test_many_clients_starting_at_once_elect_exactly_one_owner"
_ELECTION_SOAK = "LINKEDIN_MCP_ELECTION_SOAK"


def _load_daemon_election(
    *, election_soak: bool
) -> tuple[dict[str, Any], dict[str, str]]:
    real_import = builtins.__import__
    environment = {_ELECTION_SOAK: "1"} if election_soak else {}
    simulated_os = SimpleNamespace(name="nt", environ=environment)
    simulated_sys = SimpleNamespace(
        implementation=SimpleNamespace(name="cpython"),
        platform="win32",
        version_info=(3, 12, 10, "final", 0),
    )

    def simulated_import(
        name: str,
        globals: dict[str, Any] | None = None,
        locals: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if level == 0 and name == "os":
            return simulated_os
        if level == 0 and name == "sys":
            return simulated_sys
        return real_import(name, globals, locals, fromlist, level)

    module_builtins = vars(builtins).copy()
    module_builtins["__import__"] = simulated_import
    namespace: dict[str, Any] = {
        "__builtins__": module_builtins,
        "__file__": str(_TEST_MODULE),
        "__name__": "simulated_windows_312_daemon_election",
    }

    source = _TEST_MODULE.read_text(encoding="utf-8")
    exec(compile(source, str(_TEST_MODULE), "exec"), namespace)
    return namespace, environment


def test_eight_client_stress_has_no_windows_312_collection_skip() -> None:
    """The product-CI stress stays runnable on Windows CPython 3.12."""
    namespace, _ = _load_daemon_election(election_soak=False)

    owner = namespace["TestRealOwner"]
    target = owner.__dict__[_TARGET]
    marks = [
        *namespace.get("pytestmark", ()),
        *getattr(owner, "pytestmark", ()),
        *getattr(target, "pytestmark", ()),
    ]
    assert not [mark for mark in marks if mark.name in {"skip", "skipif"}]


@pytest.mark.parametrize("election_soak", [False, True])
def test_inspect_owner_environment_preserves_the_collection_gate(
    election_soak: bool,
) -> None:
    namespace, environment = _load_daemon_election(election_soak=election_soak)

    # The autouse environment fixture removes every LINKEDIN* variable after
    # collection and before the test body starts.
    environment.clear()
    child_environment = namespace["_inspect_owner_environment"]()

    assert (_ELECTION_SOAK in child_environment) is election_soak
    assert child_environment["PYTHONPATH"] == str(_TEST_MODULE.parents[1])
