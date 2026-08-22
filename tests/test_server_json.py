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
import shlex
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
from packaging.version import Version

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SERVER_JSON = _REPO_ROOT / "server.json"
_README = _REPO_ROOT / "README.md"
_RELEASE_WORKFLOW = (_REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(
    encoding="utf-8"
)
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


def _joined_instructions(stage: str) -> list[str]:
    """Dockerfile instructions with continuations joined and comments dropped.

    A ``LABEL`` may be wrapped across lines with a trailing backslash, and a
    comment line may carry the same text without setting anything.
    """
    instructions: list[str] = []
    pending = ""
    for line in stage.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            pending += f"{stripped[:-1].strip()} "
            continue
        instructions.append(" ".join(f"{pending}{stripped}".split()))
        pending = ""
    if pending:
        instructions.append(" ".join(pending.split()))
    return instructions


def test_dockerfile_labels_the_same_name(server: dict[str, Any]) -> None:
    """OCI ownership is proved by a label on the runtime stage.

    The builder stage's labels do not ship, so a label placed there passes
    review and fails verification.
    """
    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    key = "io.modelcontextprotocol.server.name"
    # Instruction names are case-insensitive to Docker, so a lowercase final
    # stage is still the one whose labels ship.
    stages = re.split(r"(?im)^FROM\s", dockerfile)
    runtime_stage = stages[-1]

    # Every assignment of this key, wherever it sits: a comment sets nothing, one
    # LABEL may carry several keys, and Docker takes the last write.
    setters = [
        token.partition("=")[2]
        for line in _joined_instructions(runtime_stage)
        if line.upper().startswith("LABEL ")
        for token in shlex.split(line[len("LABEL ") :])
        if token.startswith(f"{key}=")
    ]
    assert setters == [server["name"]], (
        f"the final stage must set {key} exactly once, to {server['name']!r}, "
        f"as an instruction rather than a comment. Found: {setters!r}"
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
    that PR merges. Requiring equality against ``pyproject.toml`` here therefore
    fails the bump PR, which blocks the merge, which stops the job that would
    have made it equal.

    Ahead is the state that cannot be reached honestly, because nothing writes
    this file except that job and a person.
    """
    assert Version(server["version"]) <= Version(pyproject["project"]["version"])


def test_the_file_moves_with_the_bundle(
    server: dict[str, Any], manifest: dict[str, Any]
) -> None:
    """An upper bound alone lets a missed sync sit here forever.

    Behind ``pyproject.toml`` is legitimate only until the release job runs, and
    nothing above says when that was. ``manifest.json`` does: the same job, the
    same commit, and it is equally behind during the bump PR. So the two are
    equal in every honest state, and a release that skipped this file alone
    breaks that equality instead of passing quietly for the rest of the
    project's life.
    """
    assert server["version"] == manifest["version"]


def test_only_package_types_the_release_job_versions_are_declared(
    server: dict[str, Any],
) -> None:
    """A third package type would stop the release after PyPI has published.

    ``prepare-release`` runs after ``publish-pypi``, so a type the rewrite has
    no branch for ends the workflow with the distribution already immutable on
    PyPI and with no tag, image, bundle or release beside it. Adding a package
    here is the moment to notice, and the two the job knows are named below
    rather than grepped out of it: searching the step for the string proves
    only that the string is there, and a branch emptied to `pass` passed.
    """
    declared = {package["registryType"] for package in server["packages"]}
    assert declared == {"pypi", "oci"}, (
        f"{sorted(declared - {'pypi', 'oci'})} would reach a release job that "
        "cannot version them; teach the `Update server.json version` step first"
    )


def test_the_mount_uses_a_flag_the_gateway_translates(server: dict[str, Any]) -> None:
    """Docker MCP Gateway reads this entry, and it knows two spellings.

    ``extractVolumesFromRuntimeArgs`` in ``pkg/catalog/registry_to_catalog.go``
    switches on the argument name and handles ``-v`` and ``--mount``. Docker
    itself accepts ``--volume`` as well, so the entry looks right and validates,
    and the gateway then drops the argument while still collecting the path from
    the user: a configured server with no mount, which starts, answers, and is
    logged out on every launch.
    """
    translated = {"-v", "--mount"}
    mounts = [
        argument
        for argument in _package(server, "oci").get("runtimeArguments", [])
        if "/home/pwuser/.linkedin-mcp" in argument.get("value", "")
    ]
    # Exactly one, because an entry with no mount at all starts logged out on
    # every launch, which is what the mount is for.
    assert len(mounts) == 1, (
        "the OCI package must mount the session directory exactly once, found "
        f"{len(mounts)}"
    )
    assert mounts[0]["name"] in translated, (
        f"{mounts[0]['name']!r} is a valid Docker flag that the gateway "
        f"does not translate; use one of {sorted(translated)}"
    )


def test_no_variable_defaults_to_a_path_only_a_shell_could_read(
    server: dict[str, Any],
) -> None:
    """A registry client substitutes and executes; nothing expands a tilde.

    The schema defines ``{variable}`` substitution and no home-directory rule,
    and clients are told to execute directly rather than through a shell. A
    default of ``~/.linkedin-mcp`` therefore reaches Docker verbatim, which
    refuses it: it is neither an absolute path nor a legal volume name, so
    every user who accepts the offered default cannot start the server at all.
    A required variable with no default makes the client ask instead.
    """
    for package in server["packages"]:
        for argument in package.get("runtimeArguments", []):
            for name, variable in argument.get("variables", {}).items():
                default = variable.get("default")
                if variable.get("format") == "filepath" and variable.get("isRequired"):
                    # Nothing written here is a path on the machine that will
                    # run it, so a required one has no honest default and the
                    # client has to ask. Rejecting only `~` let `$HOME` through,
                    # which Docker refuses in exactly the same way.
                    assert default is None, (
                        f"{name} is a required host path; offering {default!r} "
                        "puts a value there that cannot be right"
                    )
                    continue
                if not isinstance(default, str):
                    continue
                assert not default.startswith("~") and "$" not in default, (
                    f"{name} offers {default!r}, which only a shell expands"
                )


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
