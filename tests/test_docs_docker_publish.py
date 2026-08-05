"""The documented Docker HTTP command must publish to loopback.

Documentation is not usually worth a test. This is, because the command is
copied verbatim by people setting the server up, and the mistake it prevents is
silent: an MCP endpoint with no authentication, reachable from the whole
network, that keeps working perfectly for the person who pasted it. Nothing
fails, so nothing tells them.

The server cannot catch this itself. `--host 0.0.0.0` is *required* inside a
container -- a process bound to 127.0.0.1 in there is unreachable through a
published port -- so exposure is decided entirely by the publish address on the
host, which the process never sees. The docs are the only place the difference
can be made.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_README = _REPO_ROOT / "README.md"

# The published port, as `docker run` takes it. A bare `-p 8080:8080` publishes
# on every interface; the address in front restricts it to this machine.
_PUBLISH = re.compile(r"^\s*-p\s+(?P<spec>\S+)\s*\\?\s*$", re.MULTILINE)


def _docker_http_blocks() -> list[str]:
    """Every fenced ``docker run`` that serves HTTP.

    Anchored on the command rather than on a heading, because the README has
    three blocks headed "HTTP Mode Example" and only the containerised one is at
    risk. This also catches a second Docker example added somewhere else later,
    which a heading-anchored test would walk straight past.
    """
    text = _README.read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)```", text, re.DOTALL)
    matched = [
        block
        for block in blocks
        if "docker run" in block and "--transport streamable-http" in block
    ]
    assert matched, (
        f"{_README.name} no longer documents a Docker HTTP command. If it moved, "
        "point this test at it rather than deleting it: the mistake it guards "
        "against is silent."
    )
    return matched


@pytest.mark.parametrize("block", _docker_http_blocks())
def test_documented_http_command_publishes_to_loopback(block: str):
    published = [match.group("spec") for match in _PUBLISH.finditer(block)]
    assert published, f"no -p flag in the documented HTTP command:\n{block}"

    for spec in published:
        # Three fields means an address was given. Two (`8080:8080`) means
        # Docker picks every interface, which is the failure this guards.
        assert spec.count(":") >= 2, (
            f"documented HTTP command publishes {spec!r}, which Docker exposes "
            "on every interface. Publish to loopback instead, e.g. "
            "-p 127.0.0.1:8080:8080."
        )
        # Literal addresses only. A name would have to be resolved, and
        # `localhost` can carry both 127.0.0.1 and ::1, so what a reader ends up
        # bound to depends on their resolver. A documented command should not
        # leave that open.
        host = spec.rsplit(":", 2)[0]
        assert host in {"127.0.0.1", "[::1]"}, (
            f"documented HTTP command publishes on {host!r}. Use a literal "
            "loopback address: the MCP endpoint has no authentication, so what "
            "this resolves to is the whole guarantee."
        )


@pytest.mark.parametrize("block", _docker_http_blocks())
def test_documented_http_command_still_binds_the_wildcard(block: str):
    """The other half, which is just as easy to 'fix' wrongly.

    Someone reading the loopback publish above may conclude the server should
    also bind 127.0.0.1. Inside a container that makes it unreachable through
    the published port, and the symptom is a connection refused with nothing in
    the log to explain it.
    """
    assert "--host 0.0.0.0" in block, (
        "the documented Docker HTTP command must keep --host 0.0.0.0: a "
        "container-loopback bind is unreachable through a published port."
    )


@pytest.mark.parametrize("path", ["README.md", "docs/docker-hub.md"])
def test_exposure_tradeoff_is_explained_not_just_shown(path: str):
    """A reader who changes the command should know what they are giving up."""
    text = (_REPO_ROOT / path).read_text(encoding="utf-8")
    assert "127.0.0.1:8080:8080" in text, (
        f"{path} should show the loopback publish form so the safe command is "
        "the one people copy"
    )
