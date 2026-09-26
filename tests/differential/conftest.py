"""Opt-in and fixtures shared by the differential harness."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from differential.synthetic_origin import (
    ALLOWED_HOSTS,
    CA_DIR_ENV,
    CA_FILE,
    LEAF_FILE,
    LEAF_KEY_FILE,
    OPT_IN_ENV,
    EgressProxy,
    SyntheticOrigin,
)

#: Read at import, because ``ignore_the_developers_environment`` deletes every
#: ``LINKEDIN*`` variable before each test runs.
_CA_DIR = os.environ.get(CA_DIR_ENV)


@pytest.fixture
def certificates() -> Path:
    """The run's issued certificates. A missing one fails, since opting in
    without them is a broken CI step rather than a reason to skip."""
    raw = _CA_DIR
    if not raw:
        pytest.fail(f"{OPT_IN_ENV} is set but {CA_DIR_ENV} is not")
    directory = Path(raw)
    missing = [
        name
        for name in (CA_FILE, LEAF_FILE, LEAF_KEY_FILE)
        if not (directory / name).is_file()
    ]
    if missing:
        pytest.fail(f"{CA_DIR_ENV}={directory} lacks {', '.join(missing)}")
    return directory


@pytest.fixture
def synthetic_egress(
    certificates: Path,
) -> Iterator[tuple[SyntheticOrigin, EgressProxy]]:
    """The synthetic origin, and the fail-closed proxy that is the only way to it."""
    origin = SyntheticOrigin(certificates)
    origin.start()
    try:
        proxy = EgressProxy({host: origin.port for host in ALLOWED_HOSTS})
        proxy.start()
        try:
            yield origin, proxy
        finally:
            proxy.stop()
    finally:
        origin.stop()
