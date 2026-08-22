"""Rules about ``server.json`` that only a reader of the registry can know.

``server.json`` is this server's entry for the *official* MCP Registry at
``registry.modelcontextprotocol.io``, which is a service published to with the
``mcp-publisher`` CLI. It is not the Docker MCP Catalog, whose entry for this
server is a ``server.yaml`` in the ``docker/mcp-registry`` repository and is
updated by opening a PR there. The two are routinely confused, including once
on the branch that added this file, so the distinction is written down rather
than assumed.

Nothing in the release publishes this file, so nothing in the release notices
when it goes wrong either: it can sit in the repository for months naming
something that was never released, and the first sign is a rejected publish.

The registry's own checks are the reason most of these rules exist. Publishing
proves that the publisher owns each package, and it proves it differently per
registry type. For PyPI it fetches the version's metadata and searches the
README, which PyPI stores as the package description, for the literal token
``mcp-name: <server name>``. For an OCI image it pulls the image *config* and
reads ``Config.Labels["io.modelcontextprotocol.server.name"]``, which is what a
Dockerfile ``LABEL`` writes. Manifest annotations are a different surface and
are not consulted, so a label replaced by an annotation would inspect correctly
and still fail verification. Both compare against the ``name`` in this file, so
three places have to agree and only one of them is JSON.

The token check is boundary-anchored, which is the part that surprises people:
a README declaring ``mcp-name: io.github.acme/widget-pro`` would otherwise
satisfy an ownership claim for the shorter ``io.github.acme/widget``, because
the shorter string is a substring of the longer one. The registry therefore
requires the matched name to be followed by end-of-content, a character that
cannot appear in a server name, or an HTML comment close. A token written
inline and ended with a period fails that, and the failure arrives as an
ownership error rather than a formatting one. Measured against
``internal/validators/registries/mcpname.go`` and ``oci.go``.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
from packaging.version import Version

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SERVER_JSON = _REPO_ROOT / "server.json"
_README = _REPO_ROOT / "README.md"
_DOCKERFILE = _REPO_ROOT / "Dockerfile"

# The GitHub namespace the registry grants after a GitHub login. It is derived
# from the account, not chosen, so a name outside it cannot be published at all.
_NAMESPACE = "io.github.stickerdaniel"

# Characters that may continue a server name, from the schema's own pattern
# ^[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+$ plus the separating slash.
_SERVER_NAME_CHAR = re.compile(r"[A-Za-z0-9._/-]")


@pytest.fixture(scope="module")
def server() -> dict[str, Any]:
    return json.loads(_SERVER_JSON.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def pyproject() -> dict[str, Any]:
    return tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return json.loads((_REPO_ROOT / "manifest.json").read_text(encoding="utf-8"))


def _package(server: dict[str, Any], registry_type: str) -> dict[str, Any]:
    matches = [p for p in server["packages"] if p["registryType"] == registry_type]
    assert len(matches) == 1, f"expected exactly one {registry_type} package"
    return matches[0]


def _terminates_token(rest: str) -> bool:
    """Whether what follows a matched server name ends the token.

    Mirrors ``isMCPNameBoundary``. The comment-close cases are not decoration:
    ``<!-- mcp-name: NAME-->`` with no space before the close is common enough
    that the registry special-cases it, and without them the hidden-comment form
    the PyPI docs recommend would fail its own validator.
    """
    if not rest:
        return True
    if not _SERVER_NAME_CHAR.match(rest[0]):
        return True
    return rest.startswith("-->") or rest.startswith("--!>")


def test_name_sits_in_the_authenticated_namespace(server: dict[str, Any]) -> None:
    assert server["name"].startswith(f"{_NAMESPACE}/")


def test_description_fits_the_registry_limit(server: dict[str, Any]) -> None:
    """The schema caps ``description`` at 100 characters.

    A longer one is refused at publish time, which is the worst moment to find
    out: the publish is the one step that is taken deliberately and rarely.
    """
    assert len(server["description"]) <= 100


def test_readme_carries_a_terminated_ownership_token(server: dict[str, Any]) -> None:
    """PyPI ownership is proved by a token in the README, or not at all."""
    name = server["name"]
    token = f"mcp-name: {name}"
    readme = _README.read_text(encoding="utf-8")

    occurrences = [m.end() for m in re.finditer(re.escape(token), readme)]
    assert occurrences, f"README.md must contain {token!r}"
    assert any(_terminates_token(readme[end:]) for end in occurrences), (
        f"README.md contains {token!r}, but every occurrence is glued to a "
        "character that continues a server name. Put it on its own line, or "
        "close the HTML comment right after it."
    )


def test_readme_is_the_published_package_description(pyproject: dict[str, Any]) -> None:
    """The token only reaches PyPI because README.md *is* the description.

    Point ``readme`` somewhere else and the token stays in the repository while
    the published package loses it, which reads as an ownership failure with no
    visible cause.
    """
    assert pyproject["project"]["readme"] == "README.md"


def test_dockerfile_annotates_the_same_name(server: dict[str, Any]) -> None:
    """OCI ownership is proved by an annotation on the runtime stage.

    The builder stage's labels do not ship, so a label placed there passes
    review and fails verification.
    """
    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    label = f'LABEL io.modelcontextprotocol.server.name="{server["name"]}"'
    runtime_stage = dockerfile.rsplit("\nFROM ", 1)[-1]

    # A substring search is satisfied by `# LABEL ...`, which writes nothing
    # into the image. Commenting the line out while debugging would leave CI
    # green and the proof missing, and that only surfaces after the image has
    # shipped.
    instructions = [
        line
        for line in runtime_stage.splitlines()
        if line.strip() == label and not line.lstrip().startswith("#")
    ]
    assert instructions, (
        f"the final stage must carry {label!r} as an instruction, not as a comment"
    )


def test_pypi_package_is_the_published_distribution(
    server: dict[str, Any], pyproject: dict[str, Any]
) -> None:
    package = _package(server, "pypi")
    assert package["identifier"] == pyproject["project"]["name"]


def test_versions_agree_inside_the_file(server: dict[str, Any]) -> None:
    """The three version sites move together or the sync missed one.

    ``server.json`` spells the version three times, once inside an image
    reference. The release job rewrites all three; a hand edit reaches whichever
    one the author was looking at.
    """
    version = server["version"]
    assert _package(server, "pypi")["version"] == version
    assert _package(server, "oci")["identifier"].endswith(f":{version}")


def test_the_file_never_runs_ahead_of_the_project(
    server: dict[str, Any], pyproject: dict[str, Any]
) -> None:
    """Behind is the normal state between a bump and its release commit.

    ``uv version --bump`` changes ``pyproject.toml`` and ``uv.lock`` and nothing
    else, and ``server.json`` catches up in the ``prepare-release`` job after
    that PR merges. Requiring equality here therefore fails the bump PR, which
    blocks the merge, which stops the job that would have made it equal:
    ``manifest.json`` and ``docker-compose.yml`` are synced the same way and
    carry no such rule for exactly this reason.

    Ahead is the state that cannot be reached honestly, because nothing writes
    this file except that job and a person.
    """
    assert Version(server["version"]) <= Version(pyproject["project"]["version"])


def test_oci_identifier_names_the_published_image(server: dict[str, Any]) -> None:
    repository, _, _tag = _package(server, "oci")["identifier"].rpartition(":")
    assert repository == "docker.io/stickerdaniel/linkedin-mcp-server"


def test_proxy_settings_match_the_bundle(
    server: dict[str, Any], manifest: dict[str, Any]
) -> None:
    """Every package offers the configuration the MCP bundle already asks for.

    The bundle collects four proxy fields from the user. A registry client has
    no other way to learn that this server takes any configuration at all, so
    the two lists have to describe the same server. Dropping one here would not
    break anything visibly; it would just make the registry entry quietly less
    capable than the bundle.
    """
    expected = {
        name.upper() for name in manifest["user_config"] if name.startswith("proxy_")
    }
    assert expected, "manifest.json no longer declares proxy fields"

    for package in server["packages"]:
        declared = {v["name"] for v in package.get("environmentVariables", [])}
        assert declared == expected, (
            f"{package['registryType']} package declares {sorted(declared)}, "
            f"manifest.json asks for {sorted(expected)}"
        )


def test_file_is_in_the_form_the_release_job_writes(server: dict[str, Any]) -> None:
    """Hand edits keep the format the version sync produces.

    That job reparses the file and writes it back with ``json.dumps(indent=2)``.
    If the checked-in file is formatted any other way, the release commit
    carries a whole-file reformat on top of the version change, and the diff
    that should be three lines stops being reviewable.
    """
    canonical = json.dumps(server, indent=2, ensure_ascii=False) + "\n"
    assert _SERVER_JSON.read_text(encoding="utf-8") == canonical
