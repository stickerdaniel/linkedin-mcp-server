"""The image's browser and process topology are part of its behaviour.

Building the real image is too expensive for the ordinary unit suite, and
string checks are enough for the pieces that can disappear silently: the
browser product, the launch mode, and who receives SIGTERM. The supervisor
itself is exercised with fake Xvfb and server processes, because child liveness
is behaviour a string check cannot prove.

Two things resist that treatment and are measured instead, because inferring
them from the Dockerfile was tried and kept producing checks that passed while
the build did something else.

Which backend builds the project is read off the wheel the builder stage
produces. Which paths reach the build context is asked of BuildKit through a
throwaway probe, since `.dockerignore` semantics survived no reimplementation
here without diverging quietly. Together they cost seconds against a warm
cache, and they skip locally without a daemon while failing in CI, because a
gate that skips itself is indistinguishable from one that passed.

What stays a string check is what a build cannot show: that verification was
not switched off, and that the constraint files still agree with each other.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import textwrap
import tomllib
import uuid
from pathlib import Path
from typing import NoReturn

import pytest
from packaging.requirements import Requirement
from packaging.utils import NormalizedName, canonicalize_name


def _no_daemon(reason: str) -> NoReturn:
    """Fail in CI, skip locally. Never quietly pass.

    These two are the only checks that observe what the build actually does,
    so a run without a daemon has verified nothing about it. Locally that is a
    fair trade and the reason says so. In CI it is the whole point, and a
    silently skipped gate reads exactly like a passing one: the first CI run
    of these tests could not be told apart from a run where they never
    executed. Same reasoning as `tests/test_browser_identity.py`.
    """
    if os.environ.get("CI", "").lower() in {"1", "true", "yes"}:
        pytest.fail(
            f"{reason}. In CI this is a failure rather than a skip: these "
            f"tests are the only ones that measure the build instead of "
            f"reading the Dockerfile, so skipping them checks nothing."
        )
    pytest.skip(reason)


def _pinned_requirements(path: Path) -> dict[NormalizedName, tuple[Requirement, str]]:
    """The requirements a constraint file pins, by canonical name.

    The second half of each value is the entry as written, which is where the
    hashes are: they are not part of PEP 508 and `Requirement` cannot hold
    them.
    """
    parsed = {}
    for entry in _logical_lines(path.read_text(encoding="utf-8")):
        requirement = Requirement(entry.split("--hash", 1)[0].strip())
        parsed[canonicalize_name(requirement.name)] = (requirement, entry)
    return parsed


def _logical_lines(text: str) -> list[str]:
    """Join backslash continuations and drop comments.

    Both the Dockerfile and the compiled constraint file wrap one logical
    statement across several physical lines. Matching a flag against the raw
    text therefore also matches the comment that explains the flag, which is
    how an assertion on ``--no-deps`` stayed green after the flag was removed
    from the command it guards.

    Runs of whitespace collapse, because a shell reads `uv  sync` and
    `uv sync` alike and a search for the second missed the first entirely,
    letting a whole unconstrained sync hide behind one extra space.

    Line continuation is the rest of what this handles, and
    `test_the_dockerfile_stays_within_the_syntax_this_file_parses` refuses the
    constructs where that is not enough. A heredoc would otherwise present its
    payload as instructions, and BuildKit reads a `\\\\` line ending and a
    `# escape=` directive differently again.
    """
    statements: list[str] = []
    pending = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            pending += f"{stripped[:-1].strip()} "
            continue
        statements.append(" ".join(f"{pending}{stripped}".split()))
        pending = ""
    if pending:
        statements.append(" ".join(pending.split()))
    return statements


# The two commands that decide which backend builds the project, written out
# whole. Reading a flag out of them needs a shell parser, and every version of
# that parser written here had a hole: a comment swallowing the rest of the
# line, a `&` keeping two commands in one string, a `$(...)` hiding one inside
# another, an `ENV=1` prefix moving the command name off the front. Comparing
# the instruction entire needs no parser and has no hole. It also makes these
# two lines deliberately awkward to change, which is the point.
_DEPENDENCY_SYNC = (
    "RUN uv sync --frozen --no-install-project --no-dev --no-editable "
    "--compile-bytecode"
)
_PROJECT_INSTALL = (
    "RUN uv pip install --python /app/.venv/bin/python --no-deps "
    "--compile-bytecode --build-constraints build-constraints.txt ."
)


# Anything that can build this project during the image build. `uv run` is the
# one that does not look like an install: it syncs and installs the project
# before running its command, so a smoke check added to the builder stage runs
# the backend unconstrained. Measured against a real build, which resolved the
# backend past a corrupted constraint file that the later install still failed
# on. Naming frontends is a filter rather than a proof, and the comment on its
# use says so; this is the ordinary-mistake half.
_PROJECT_BUILDING_COMMANDS = (
    "uv sync",
    "uv pip install",
    "uv run",
    "uv build",
    # `uvx` and `uv tool` are the same command under two spellings, and only
    # the short one was here at first: `uv tool install .` built the project
    # past a corrupted constraint file while this guard reported nothing.
    "uvx",
    "uv tool",
    "pip install",
    "pip wheel",
    "python -m build",
    "setup.py",
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOCKERFILE = (_REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
_DOCKERFILE_INSTRUCTIONS = _logical_lines(_DOCKERFILE)
_ENTRYPOINT_PATH = _REPO_ROOT / "docker-entrypoint.sh"
_ENTRYPOINT = _ENTRYPOINT_PATH.read_text(encoding="utf-8")
_README = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
_DOCKER_GUIDE = (_REPO_ROOT / "docs" / "docker-hub.md").read_text(encoding="utf-8")
_BUILD_CONSTRAINTS_PATH = _REPO_ROOT / "build-constraints.txt"
_PYPROJECT_PATH = _REPO_ROOT / "pyproject.toml"
# Pinned: the probe runs against the developer's own machine, and `latest`
# is whatever the registry serves that day.
_PROBE_IMAGE = (
    "busybox@sha256:dc2d74b28e4cf8984fa52af1f39bc7c3d9c73760b41a74d629f5d11b1ab28616"
)


# `test_the_built_project_records_the_pinned_backend` builds the builder stage,
# so a missing input fails it: that stage is where the pin is decided and it is
# cheap to build. `docker-entrypoint.sh` is the exception, copied in the second
# stage, which only a full image build would reach. Excluding it produced a
# green suite and a broken `docker build .`, so it is named here.
_CONTEXT_INPUTS = ("build-constraints.txt", "docker-entrypoint.sh")

# These must not reach the build. The environment names cover the root, a
# suffix and a nested directory, which is the whole of what `**/.env*` claims.
# The rest is local working material that no build reads and that `COPY . .`
# would otherwise put in a layer the release exports to the GitHub Actions
# cache.
_EXCLUDED_SENTINELS = (
    ".env",
    ".env.example",
    ".env.local",
    ".env.previous-proxy",
    "tests/.env.probe",
    "nested/deeper/.env.secret",
    ".debug/notes.md",
    "CLAUDE.local.md",
    ".agents/notes.md",
    ".claude/settings.json",
)


@pytest.fixture(scope="module")
def build_context(tmp_path_factory: pytest.TempPathFactory) -> frozenset[str]:
    """Which sentinels this `.dockerignore` lets through, asked of BuildKit.

    The semantics are not recoverable from the file alone. Every
    reimplementation measured here disagreed with BuildKit while staying
    silent about it: a leading slash anchors the pattern, `*` stops at a
    separator, `\\!` escapes a negation, and a wildcard such as
    `build-constraints.*` hides a file that a literal-name search still reads
    as present. Each of those is a false pass, and a dropped constraint file
    costs more than a red build: the release publishes to PyPI and tags the
    repository before the Docker job runs, so a version can exist on PyPI and
    as a tag with no image behind it.

    The context is synthetic rather than the repository itself. Building the
    real root sent 310 files to the daemon on a developer checkout, 32 of them
    under `.debug/`, plus `CLAUDE.local.md`, and `docker rmi` does not
    necessarily drop the cached `COPY` layer. An ordinary test run has no
    business copying those anywhere. The probe image is pinned by digest and
    runs without a network for the same reason.
    """
    docker = shutil.which("docker")
    if docker is None:
        _no_daemon("docker is required to read the real build context")
    # Unavailability is what may skip. Once the daemon answers and the base
    # image is local, a failing probe is a failing probe: treating every
    # non-zero exit as absence turned a malformed `.dockerignore` into a green
    # run that had checked nothing.
    for available in ([docker, "info"], [docker, "pull", "-q", _PROBE_IMAGE]):
        probe = subprocess.run(
            available, capture_output=True, text=True, timeout=300, check=False
        )
        if probe.returncode != 0:
            _no_daemon(f"no usable docker daemon: {probe.stderr.strip()[:200]}")

    context = tmp_path_factory.mktemp("dockerignore-probe")
    shutil.copyfile(_REPO_ROOT / ".dockerignore", context / ".dockerignore")
    for sentinel in _CONTEXT_INPUTS + _EXCLUDED_SENTINELS:
        path = context / sentinel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("sentinel\n", encoding="utf-8")

    # The pid alone collides across clients sharing one daemon from separate
    # pid namespaces, where `rmi -f` would then remove another run's tag.
    tag = f"linkedin-mcp-context-probe:{os.getpid()}-{uuid.uuid4().hex[:12]}"
    try:
        build = subprocess.run(
            [docker, "build", "-q", "-t", tag, "-f", "-", str(context)],
            input=f"FROM {_PROBE_IMAGE}\nCOPY . /ctx\n",
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        assert build.returncode == 0, f"context probe failed: {build.stderr[-2000:]}"
        listing = subprocess.run(
            # fmt: off
            [
                docker,
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "find",
                tag,
                "/ctx",
                "-type",
                "f",
            ],
            # fmt: on
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        )
    finally:
        subprocess.run(
            [docker, "rmi", "-f", tag], capture_output=True, text=True, check=False
        )

    return frozenset(
        line.removeprefix("/ctx/")
        for line in listing.stdout.split("\n")
        if line.startswith("/ctx/")
    )


def test_the_image_installs_only_the_full_browser() -> None:
    """The shell is a different product and nothing in the image launches it."""
    assert "patchright install chromium --no-shell" in _DOCKERFILE


def test_the_image_builds_the_project_against_the_hashed_backend() -> None:
    """Installing the project runs the build backend, so the image must pin it.

    `uv sync` takes no build constraint, so a plain sync of the project resolves
    setuptools fresh from PyPI inside the one release job that holds the Docker
    Hub credentials (#655). The project is installed on its own instead, against
    the same hashed file the published distributions use (#654).

    Both commands are compared whole. A flag matched inside them says only that
    the text contains it, and every attempt here to find out whether the
    command *runs* with it needed a shell parser that turned out to have a
    hole. Changing either line therefore fails this test on purpose: the
    expected form is in the failure output, and a deliberate change updates it.
    """
    assert _DEPENDENCY_SYNC in _DOCKERFILE_INSTRUCTIONS, _DEPENDENCY_SYNC
    assert _PROJECT_INSTALL in _DOCKERFILE_INSTRUCTIONS, _PROJECT_INSTALL
    # Any further build of the project is unaccounted for: a second one runs
    # the backend unconstrained, and a constrained install afterwards cannot
    # undo a build that already ran. Naming the frontends is a filter, not a
    # proof; something determined to hide a build can. What it is here for is
    # the ordinary case, someone adding a second install without noticing that
    # the first one is load-bearing.
    # `RUN` only: an instruction that executes nothing at build time cannot
    # build the project. The `COPY` that brings the uv binaries in names `uvx`
    # in its source path and would otherwise read as one of these.
    builders = [
        instruction
        for instruction in _DOCKERFILE_INSTRUCTIONS
        if instruction.startswith("RUN ")
        and any(frontend in instruction for frontend in _PROJECT_BUILDING_COMMANDS)
    ]
    assert builders == [_DEPENDENCY_SYNC, _PROJECT_INSTALL], builders
    # `--no-verify-hashes` has an environment twin that reaches every uv
    # invocation in the stage and turns the same verification off. The wheel
    # this produces would still name the pinned version, so the measurement
    # cannot see this one and the string check has to.
    assert "UV_NO_VERIFY_HASHES" not in _DOCKERFILE


def test_the_dockerfile_stays_within_the_syntax_this_file_parses() -> None:
    """`_logical_lines` handles line continuation, so nothing else may appear.

    Only the three constructs that change what an *instruction* is are refused,
    because that is all the comparison above depends on. A heredoc is data to
    BuildKit and instructions here, so a payload line spelling one of the two
    commands would stand in for the real one. A line ending in `\\` ends the
    instruction for BuildKit and continues it here. An `escape` directive
    changes the continuation character entirely.

    Nothing else about the shell is restricted. An earlier version refused
    quotes, `$`, backticks and a single `&` so that a hand-written shell split
    stayed sound; it was both incomplete, missing an `ENV=1 uv sync` prefix,
    and in the way of ordinary things like a cache mount. Comparing the two
    instructions whole removed the need for it.
    """
    assert "<<" not in _DOCKERFILE
    assert not re.search(r"^\s*#\s*escape\s*=", _DOCKERFILE, re.M)
    assert not [
        line for line in _DOCKERFILE.splitlines() if line.rstrip().endswith("\\\\")
    ]


def test_every_build_requirement_is_pinned_with_hashes() -> None:
    """The constraint files have to cover all of `[build-system].requires`.

    Asserting only that some hashed `setuptools==` entry exists let an added
    `wheel` install unpinned while both files still read as pinned. The source
    file is checked alongside the compiled one because it is what carries the
    transitive closure: `uv pip compile` writes every dependency of what it is
    given, so a requirement present there cannot bring an unhashed dependency
    along, and one added only to the compiled file can. Nothing offline can
    prove the compiled file is the current output of that command; Renovate's
    `pip-compile` manager regenerates it, and the hash check below is what
    catches a partial edit.
    """
    build_system = tomllib.loads(_PYPROJECT_PATH.read_text(encoding="utf-8"))[
        "build-system"
    ]
    requirements = [Requirement(entry) for entry in build_system["requires"]]
    required = {canonicalize_name(requirement.name) for requirement in requirements}
    assert required
    # An extra pulls its own dependencies into the isolated build environment,
    # and the name alone says nothing about them. `setuptools[core]` resolved
    # an unhashed wheel past a green run of this test.
    extras = sorted(
        f"{requirement.name}[{','.join(sorted(requirement.extras))}]"
        for requirement in requirements
        if requirement.extras
    )
    assert not extras, (
        f"build requirements with extras need their own hashed entries: {extras}"
    )

    sources = _pinned_requirements(_REPO_ROOT / "build-constraints.in")
    compiled = _pinned_requirements(_BUILD_CONSTRAINTS_PATH)

    assert required <= sources.keys(), (
        f"build requirements missing from build-constraints.in: "
        f"{sorted(required - sources.keys())}"
    )
    assert sources.keys() <= compiled.keys(), (
        f"sources missing from the compiled file: "
        f"{sorted(sources.keys() - compiled.keys())}"
    )
    unhashed = sorted(
        name for name, (_, entry) in compiled.items() if "--hash=sha256:" not in entry
    )
    assert not unhashed, f"compiled entries without a hash: {unhashed}"

    # A half-applied bump moves the source and leaves the compiled file behind,
    # and everything above still holds: the name is present and it has hashes.
    # The build then keeps using the old version with a green suite.
    drifted = {
        name: (str(source.specifier), str(compiled[name][0].specifier))
        for name, (source, _) in sources.items()
        if compiled[name][0].specifier != source.specifier
    }
    assert not drifted, f"build-constraints.in and .txt disagree: {drifted}"


@pytest.mark.image_build
def test_the_hashed_build_constraint_reaches_the_build_context(
    build_context: frozenset[str],
) -> None:
    """An excluded constraint file drops a pin the build never asked for."""
    missing = sorted(name for name in _CONTEXT_INPUTS if name not in build_context)
    assert not missing, f"the build needs these and .dockerignore drops them: {missing}"


@pytest.mark.image_build
def test_the_build_context_carries_nothing_it_should_not(
    build_context: frozenset[str],
) -> None:
    """A suffixed `.env` carries the same secrets as the plain one.

    `COPY . .` keeps the whole context in the builder layer, and the release
    workflow exports that layer through `cache-to: type=gha,mode=max`. Ignoring
    only `.env` let a local `.env.previous-proxy` through, holding a LinkedIn
    session cookie and proxy credentials; a real build listed it inside `/app`.
    The same build carried `.debug/` and `CLAUDE.local.md`, 9.7 MB of local
    working material that nothing in the image reads. No published image
    carried any of it: the runtime stage takes only `/app/.venv`.
    """
    leaked = sorted(path for path in _EXCLUDED_SENTINELS if path in build_context)
    assert not leaked, f"these reached the build context: {leaked}"


@pytest.mark.image_build
def test_the_built_project_records_the_pinned_backend() -> None:
    """Which setuptools built the project, read off the wheel it produced.

    This observes the property #655 is about rather than inferring it from the
    Dockerfile, which six rounds of inferring showed the value of: a comment, a
    `&`, an `ENV=1` prefix, an extra space, an inert marker and a second
    frontend each passed an inference while the build did something else.

    The version alone would not be enough. `build-constraints.txt` normally
    pins whatever is current, and Renovate keeps it that way, so an
    unconstrained build resolves the same number and produces the same wheel.
    It reads as proof exactly when it proves nothing. The second build settles
    it: the constraint is corrupted and the build has to fail on it, which no
    build that ignores the file can do. Together they say the file was read and
    that what it names is what ran.

    It also settles most of the context: a `COPY` source missing from this
    stage fails the build rather than passing a check that never knew to look
    for it. Only the second stage's own input is left to `_CONTEXT_INPUTS`.

    The builder stage alone, because the full image downloads Chromium and
    this runs on every push. The whole repository is the context, as it is for
    `docker build .`, which is why the cheap `.dockerignore` probe uses a
    synthetic one instead.
    """
    docker = shutil.which("docker")
    if docker is None:
        _no_daemon("docker is required to build the image")
    if subprocess.run([docker, "info"], capture_output=True, check=False).returncode:
        _no_daemon("no usable docker daemon")

    build_system = tomllib.loads(_PYPROJECT_PATH.read_text(encoding="utf-8"))[
        "build-system"
    ]
    required = {
        canonicalize_name(Requirement(entry).name) for entry in build_system["requires"]
    }
    backend, _ = next(
        pinned
        for name, pinned in _pinned_requirements(_BUILD_CONSTRAINTS_PATH).items()
        if name in required
    )
    expected = str(backend.specifier).removeprefix("==")

    tag = f"linkedin-mcp-backend-probe:{os.getpid()}-{uuid.uuid4().hex[:12]}"
    try:
        build = subprocess.run(
            # fmt: off
            [
                docker,
                "build",
                "-q",
                "--target",
                "builder",
                "-t",
                tag,
                str(_REPO_ROOT),
            ],
            # fmt: on
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
        assert build.returncode == 0, f"image build failed: {build.stderr[-3000:]}"
        wheel = subprocess.run(
            # fmt: off
            [
                docker,
                "run",
                "--rm",
                "--entrypoint",
                "sh",
                tag,
                "-c",
                "cat /app/.venv/lib/python*/site-packages/"
                "mcp_server_linkedin-*.dist-info/WHEEL",
            ],
            # fmt: on
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        )
        # Corrupt every hash and repeat the install off the stage that just
        # ran it. A build reading the constraint file cannot get past this; one
        # ignoring it finishes exactly as before.
        # Not `-q`: the reason has to be readable. Without the build log, any
        # failure at all would satisfy this, including a typo in the `sed`.
        tampered = subprocess.run(
            [docker, "build", "--progress", "plain", "-f", "-", str(_REPO_ROOT)],
            input=(
                f"FROM {tag}\n"
                "RUN sed -i s/--hash=sha256:/--hash=sha256:0/g "
                "build-constraints.txt\n"
                f"{_PROJECT_INSTALL} --force-reinstall\n"
            ),
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
    finally:
        subprocess.run(
            [docker, "rmi", "-f", tag], capture_output=True, text=True, check=False
        )

    assert f"Generator: setuptools ({expected})" in wheel.stdout, wheel.stdout
    assert tampered.returncode != 0, (
        "a corrupted constraint hash did not stop the build"
    )
    assert "Hash mismatch" in tampered.stderr, tampered.stderr[-3000:]


def test_the_image_defaults_to_headed_on_a_virtual_display() -> None:
    assert "ENV HEADLESS=false" in _DOCKERFILE
    assert "ENV DISPLAY=:99" in _DOCKERFILE
    assert 'Xvfb "$DISPLAY" -screen 0 1920x1080x24 -nolisten tcp' in _ENTRYPOINT


def test_the_image_seeds_the_default_authentication_root_for_named_volumes() -> None:
    creation = _DOCKERFILE.index(
        "RUN install -d -m 0700 -o pwuser -g pwuser /home/pwuser/.linkedin-mcp"
    )
    unprivileged_runtime = _DOCKERFILE.index("USER pwuser")

    assert creation < unprivileged_runtime


def test_the_documented_bind_mount_is_created_by_the_host_user() -> None:
    for document in (_README, _DOCKER_GUIDE):
        assert "mkdir -p ~/.linkedin-mcp" in document
        assert 'sudo chown -R "$(id -u):$(id -g)" ~/.linkedin-mcp' in document


def _documented_client_mounts(document: str) -> list[str]:
    """Every ``-v`` value an MCP client would hand to ``docker`` verbatim."""
    mounts: list[str] = []
    for block in re.findall(r"```json\n(.*?)```", document, re.DOTALL):
        for server in json.loads(block).get("mcpServers", {}).values():
            arguments = server.get("args", [])
            mounts += [
                value for flag, value in zip(arguments, arguments[1:]) if flag == "-v"
            ]
    return mounts


def test_every_documented_mount_is_a_path_docker_accepts() -> None:
    """A client executes these arrays, so Docker reads each path verbatim.

    Docker resolves a bind source only when it is already absolute; anything
    else is read as a volume *name*, whose character class ``~``, ``$HOME``
    and ``%USERPROFILE%`` all fail. Docker then exits 125 before Tini, the
    supervisor or Python ever start, and the stdio transport this container
    exists to serve is unreachable through the configuration the docs give
    for it. Measured against the daemon: ``%USERPROFILE%/.linkedin-mcp:...``
    exits 125, while an absolute path is accepted whatever else it holds -- a
    ``$`` inside one names a directory rather than a variable, because no
    shell is involved.
    """
    for name, document in (
        ("README.md", _README),
        ("docs/docker-hub.md", _DOCKER_GUIDE),
    ):
        mounts = _documented_client_mounts(document)
        written = re.findall(r'"-v",\s*"([^"]*)"', document)
        assert mounts == written, (
            f"{name} writes {written} but this check parsed {mounts}, so a "
            "configuration block was never inspected"
        )
        assert mounts, f"{name} documents no mount to check"

        for mount in mounts:
            host, _, container = mount.rpartition(":")
            assert re.match(r"(?:/|[A-Za-z]:/)", host), (
                f"{name}: {host!r} is not an absolute path, so Docker reads it "
                "as a volume name and exits 125"
            )
            assert container.startswith("/"), (
                f"{name}: {container!r} is not an absolute destination"
            )


def test_no_documented_argument_starts_with_a_tilde() -> None:
    """No client expands one, and every ``args`` array is executed directly."""
    checked = 0
    for document in (_README, _DOCKER_GUIDE):
        for block in re.findall(r"```json\n(.*?)```", document, re.DOTALL):
            for server in json.loads(block).get("mcpServers", {}).values():
                for argument in server.get("args", []):
                    checked += 1
                    assert not argument.startswith("~"), (
                        f"{argument!r} is handed to {server.get('command')!r} "
                        "without a shell, so nothing expands the tilde"
                    )
    assert checked, "no arguments found to check"


def test_the_supervisor_stops_python_before_the_display() -> None:
    """Browser cleanup must retain Xvfb until Python has exited."""
    assert 'ENTRYPOINT ["tini", "-e", "143"' in _DOCKERFILE
    assert 'ENTRYPOINT ["tini", "-g"' not in _DOCKERFILE
    assert "wait -n -p first_child" in _ENTRYPOINT
    server_term = _ENTRYPOINT.index('kill -TERM "$server_pid"')
    server_wait = _ENTRYPOINT.index('wait "$server_pid"', server_term)
    display_term = _ENTRYPOINT.index('kill -TERM "$xvfb_pid"', server_wait)
    assert server_term < server_wait < display_term
    assert "xvfb-run" not in _DOCKERFILE


def test_the_entrypoint_waits_for_the_display_socket() -> None:
    """An Xvfb process existing does not mean its socket is ready yet."""
    assert "display_number=${BASH_REMATCH[1]}" in _ENTRYPOINT
    assert 'socket_path="/tmp/.X11-unix/X${display_number}"' in _ENTRYPOINT
    assert 'rm -f -- "$socket_path" "$lock_path"' in _ENTRYPOINT
    assert "$attempt -lt 100" in _ENTRYPOINT


@pytest.mark.skipif(os.name == "nt", reason="the image entrypoint is Bash on Linux")
def test_a_host_prefixed_display_is_refused_before_xvfb_starts() -> None:
    """The image owns a Unix-socket display and never starts a TCP X server."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to exercise the Linux image entrypoint")

    process = subprocess.run(
        [bash, str(_ENTRYPOINT_PATH), "/usr/bin/true"],
        env={**os.environ, "DISPLAY": "localhost:99.0"},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert process.returncode == 2
    assert "DISPLAY must be a local X display such as :99 or :99.0" in process.stderr
    assert "Xvfb" not in process.stderr


# A display that comes up and stays up, for the tests whose subject is the
# descriptor rather than the display.
_FAKE_XVFB = """\
#!/usr/bin/env python3
import pathlib
import socket
import sys
import time

number = sys.argv[1].removeprefix(":").split(".", 1)[0]
path = pathlib.Path(f"/tmp/.X11-unix/X{number}")
# Real Xvfb 21.1.7 publishes the lock before it creates the socket. Measured
# by inotify inside the image; a fake that reverses it would let a readiness
# check that accepts the lock pass here.
pathlib.Path(f"/tmp/.X{number}-lock").write_text("owned")
server = socket.socket(socket.AF_UNIX)
server.bind(str(path))
server.listen()
time.sleep(5)
"""


def _a_free_display(base: int) -> tuple[int, Path, Path]:
    """Pick a display number whose socket and lock nobody else owns.

    These tests run the real entrypoint, which begins by removing both names,
    so a number that something else is using would have its socket deleted out
    from under it and its clients left with nowhere to connect. Deriving the
    number from the PID keeps two tests in one process apart and claims
    nothing. Scan for a free number instead, and claim it by creating its lock
    exclusively, which no second scanner can then pick.
    """
    start = base + (os.getpid() % 10_000)
    for offset in range(100):
        number = base + ((start - base + offset) % 10_000)
        socket_path = Path(f"/tmp/.X11-unix/X{number}")
        lock_path = Path(f"/tmp/.X{number}-lock")
        if socket_path.exists():
            continue
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Claims the number rather than reporting it free, so a second
            # scanner cannot pick the same one between this check and the
            # entrypoint's removal. The entrypoint deletes the lock on start,
            # which is the stale state these tests want anyway.
            os.close(os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
        except FileExistsError:
            continue
        return number, socket_path, lock_path
    pytest.fail(f"no free X display in {base}..{base + 99}")


@pytest.mark.skipif(os.name == "nt", reason="the image entrypoint is Bash on Linux")
def test_stale_x11_state_is_removed_and_xvfb_dying_stops_the_server(
    tmp_path: Path,
) -> None:
    """A hard-stopped container must restart and keep display liveness coupled.

    The stale socket and lock are what a SIGKILL leaves in the writable layer.
    Fake Xvfb refuses to start while either exists, then creates the real socket
    spelling for ``:N.0`` (``XN``) and dies cleanly. The supervisor must remove
    the stale state, notice the later death despite the child becoming a zombie,
    terminate the server, and return non-zero. ``kill -0`` keeps succeeding for
    a zombie, which is why waiting for either child is part of the contract.
    """
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to exercise the Linux image entrypoint")
    major = int(
        subprocess.check_output(
            [bash, "-c", 'printf "%s" "${BASH_VERSINFO[0]}"'], text=True
        )
    )
    if major < 5:
        pytest.skip("wait -n -p requires the Bash 5 shipped by the image")

    display_number, socket_path, lock_path = _a_free_display(10_000)
    display = f":{display_number}.0"
    stale_socket = socket.socket(socket.AF_UNIX)
    stale_socket.bind(str(socket_path))
    stale_socket.close()
    lock_path.write_text("stale", encoding="utf-8")

    # Records that it got past the stale names, which is the half of this the
    # supervisor's exit status cannot show: a stale socket satisfies the
    # readiness loop on its own, so dropping the cleanup produces the same
    # non-zero exit and the same TERM as a display that came up and then died.
    started_marker = tmp_path / "xvfb-started"
    fake_xvfb = tmp_path / "Xvfb"
    fake_xvfb.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import pathlib
            import socket
            import sys
            import time

            number = sys.argv[1].removeprefix(":").split(".", 1)[0]
            path = pathlib.Path(f"/tmp/.X11-unix/X{{number}}")
            lock = pathlib.Path(f"/tmp/.X{{number}}-lock")
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() or lock.exists():
                raise SystemExit(91)
            lock.write_text("owned", encoding="utf-8")
            # The gap real Xvfb leaves between the two names, widened so that a
            # readiness check accepting the lock would start the server against
            # a socket that does not exist yet.
            time.sleep(0.5)
            server = socket.socket(socket.AF_UNIX)
            server.bind(str(path))
            server.listen()
            pathlib.Path({str(started_marker)!r}).write_text("started")
            time.sleep(0.3)
            server.close()
            path.unlink(missing_ok=True)
            lock.unlink(missing_ok=True)
            """
        ),
        encoding="utf-8",
    )
    fake_xvfb.chmod(0o755)

    term_marker = tmp_path / "server-received-term"
    display_marker = tmp_path / "server-saw-display"
    fake_server = tmp_path / "server"
    fake_server.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import os
            import pathlib
            import signal
            import socket
            import time

            marker = pathlib.Path(os.environ["SERVER_TERM_MARKER"])

            # The display is what the supervisor waited for, so a server that
            # never touches it cannot tell readiness from a guess. Chromium
            # connects here; this connects and nothing else.
            display = os.environ["DISPLAY"].removeprefix(":").split(".", 1)[0]
            probe = socket.socket(socket.AF_UNIX)
            try:
                probe.connect(f"/tmp/.X11-unix/X{display}")
            except OSError as error:
                pathlib.Path(os.environ["SERVER_DISPLAY_MARKER"]).write_text(
                    f"unreachable: {error.errno}", encoding="utf-8"
                )
                raise SystemExit(90) from error
            probe.close()
            pathlib.Path(os.environ["SERVER_DISPLAY_MARKER"]).write_text(
                "reachable", encoding="utf-8"
            )

            def stop(*_args):
                marker.write_text("term", encoding="utf-8")
                raise SystemExit(0)

            signal.signal(signal.SIGTERM, stop)
            while True:
                time.sleep(0.1)
            """
        ),
        encoding="utf-8",
    )
    fake_server.chmod(0o755)

    env = {
        **os.environ,
        "DISPLAY": display,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "SERVER_TERM_MARKER": str(term_marker),
        "SERVER_DISPLAY_MARKER": str(display_marker),
    }
    process = subprocess.Popen(
        [bash, str(_ENTRYPOINT_PATH), str(fake_server)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            # Kill the whole fake process group, which is exactly what a bare
            # subprocess.run(timeout=...) fails to do. A broken supervisor must
            # not leave the fake server sleeping after the test has failed.
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise
    finally:
        socket_path.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)

    assert display_marker.read_text(encoding="utf-8") == "reachable", (
        "the server was started before the display could accept a connection, "
        "so the readiness check passed on something other than the socket. "
        f"marker={display_marker.read_text(encoding='utf-8')!r} stdout={stdout!r}"
    )
    assert started_marker.exists(), (
        "the stale socket and lock were not removed, so the display never came "
        f"up and the container is back in its restart loop. stdout={stdout!r} "
        f"stderr={stderr!r}"
    )
    assert process.returncode != 0, (stdout, stderr)
    assert term_marker.read_text(encoding="utf-8") == "term"


@pytest.mark.skipif(os.name == "nt", reason="the image entrypoint is Bash on Linux")
def test_the_supervisor_hands_the_server_the_containers_stdin(tmp_path: Path) -> None:
    """The stdio transport is the whole product over ``docker run -i``.

    A shell assigns ``/dev/null`` to the standard input of an asynchronous list
    in the absence of an explicit redirection, so ``"$@" &`` left the server
    reading a descriptor the container's stdin never reached. Measured against
    the published 4.23.0 image, a full MCP handshake produced nothing at all:
    the server started, announced the transport, read EOF from ``/dev/null`` at
    once and exited 0. Nothing failed, so nothing said so.

    A string check would not have caught it and did not: the supervisor already
    had tests for the display, for signal order and for child liveness, and all
    of them passed for the two releases this shipped in. So this runs the real
    script with a fake server that reports what it can read, which is the only
    form of the question that has an answer.
    """
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to exercise the Linux image entrypoint")
    major = int(
        subprocess.check_output(
            [bash, "-c", 'printf "%s" "${BASH_VERSINFO[0]}"'], text=True
        )
    )
    if major < 5:
        pytest.skip("wait -n -p requires the Bash 5 shipped by the image")

    display_number, socket_path, lock_path = _a_free_display(20_000)

    fake_xvfb = tmp_path / "Xvfb"
    fake_xvfb.write_text(_FAKE_XVFB, encoding="utf-8")
    fake_xvfb.chmod(0o755)

    # Echoes one line back and exits, so a server that is handed /dev/null ends
    # the run just as fast as one that is handed the pipe. The difference is
    # what comes out, never how long the test takes.
    fake_server = tmp_path / "server"
    fake_server.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import sys

            line = sys.stdin.readline()
            sys.stdout.write(f"server-read:{line.strip()}\\n")
            sys.stdout.flush()
            """
        ),
        encoding="utf-8",
    )
    fake_server.chmod(0o755)

    env = {
        **os.environ,
        "DISPLAY": f":{display_number}",
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
    }
    process = subprocess.Popen(
        [bash, str(_ENTRYPOINT_PATH), str(fake_server)],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        try:
            stdout, stderr = process.communicate("a-request\n", timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise
    finally:
        socket_path.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)

    assert "server-read:a-request" in stdout, (
        "the server was started without the container's stdin, so the stdio "
        f"transport can never receive a request. stdout={stdout!r} "
        f"stderr={stderr!r}"
    )
    # The reachable descriptor is half the contract. Without this the run also
    # passes when the supervisor dies on `wait -n -p` right after the fake
    # server has already echoed, leaving fake Xvfb alive behind a green test.
    assert process.returncode == 0, (stdout, stderr)


@pytest.mark.skipif(os.name == "nt", reason="the image entrypoint is Bash on Linux")
@pytest.mark.parametrize("kind", ["read_write_file", "socket", "read_write_null"])
def test_a_readable_descriptor_is_handed_over_untouched(
    tmp_path: Path, kind: str
) -> None:
    """The guard's false-positive direction is the one that costs the product.

    Condemning a descriptor the server could have read replaces it with
    ``/dev/null``, and the server then reads EOF and answers nothing: the exact
    bug this change exists to remove, restored silently and with a zero exit
    status. Every other test here hands the guard something bad and checks that
    it notices. This hands it something good.

    Access mode 2 is the gap those tests leave. The pipe behind
    ``subprocess.PIPE`` is mode 0, so a guard rewritten to reject every non-zero
    mode passed the entire file while turning away terminals, sockets and the
    read-write ``/dev/null`` that plain ``docker run`` supplies.
    """
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to exercise the Linux image entrypoint")
    major = int(
        subprocess.check_output(
            [bash, "-c", 'printf "%s" "${BASH_VERSINFO[0]}"'], text=True
        )
    )
    if major < 5:
        pytest.skip("wait -n -p requires the Bash 5 shipped by the image")
    if not Path("/proc/self/fdinfo").is_dir():
        pytest.skip("the guard reads the access mode through /proc")

    display_number, socket_path, lock_path = _a_free_display(40_000)
    fake_xvfb = tmp_path / "Xvfb"
    fake_xvfb.write_text(_FAKE_XVFB, encoding="utf-8")
    fake_xvfb.chmod(0o755)

    seen = tmp_path / "seen"
    fake_server = tmp_path / "server"
    fake_server.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import fcntl
            import os
            import pathlib
            import sys

            mode = fcntl.fcntl(0, fcntl.F_GETFL) & os.O_ACCMODE
            target = os.readlink("/proc/self/fd/0")
            first = sys.stdin.readline().strip()
            pathlib.Path({str(seen)!r}).write_text(
                f"{{target}} mode={{mode}} read={{first}}"
            )
            """
        ),
        encoding="utf-8",
    )
    fake_server.chmod(0o755)

    peer: socket.socket | None = None
    if kind == "read_write_file":
        source = tmp_path / "request"
        source.write_text("a-request\n", encoding="utf-8")
        handed = os.open(str(source), os.O_RDWR)
        expected_target, expected_read = str(source), "a-request"
    elif kind == "socket":
        near, peer = socket.socketpair()
        peer.sendall(b"a-request\n")
        handed = os.dup(near.fileno())
        near.close()
        expected_target, expected_read = None, "a-request"
    else:
        handed = os.open(os.devnull, os.O_RDWR)
        expected_target, expected_read = os.devnull, ""

    env = {
        **os.environ,
        "DISPLAY": f":{display_number}",
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
    }
    try:
        process = subprocess.Popen(
            [bash, str(_ENTRYPOINT_PATH), str(fake_server)],
            env=env,
            stdin=handed,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise
    finally:
        os.close(handed)
        if peer is not None:
            peer.close()
        socket_path.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)

    assert seen.exists(), (stdout, stderr)
    answer = seen.read_text(encoding="utf-8")
    # Mode 2 in the answer is the whole point: a replaced descriptor reads back
    # as `/dev/null mode=0`, which is indistinguishable from the original by
    # name alone in the third case.
    assert f"mode={os.O_RDWR} read={expected_read}" in answer, (
        f"the server was handed {answer!r} rather than the descriptor it was "
        f"started with. stderr={stderr!r}"
    )
    if expected_target is not None:
        assert answer.startswith(f"{expected_target} "), answer
    assert process.returncode == 0, (stdout, stderr)


@pytest.mark.skipif(os.name == "nt", reason="the image entrypoint is Bash on Linux")
@pytest.mark.parametrize(
    "kind", ["closed", "directory", "write_only", "no_access_mode", "path_only"]
)
def test_an_unusable_standard_input_becomes_dev_null(tmp_path: Path, kind: str) -> None:
    """Spelling the redirection out is what stops hiding a bad descriptor.

    The bare ``&`` this replaces gave every mode ``/dev/null`` whether it wanted
    one or not, so nothing here noticed what a launcher may actually pass.
    Handing it through unexamined is worse than either. A closed descriptor, a
    write-only one, one opened in Linux's fourth access mode and an ``O_PATH``
    handle all reach Python as ``EBADF`` on the first read, and an open
    directory ends the interpreter before argument parsing, taking down HTTP and
    the one-shot commands over an input none of them reads.

    The closed case does not arrive this way in the shipped image, because Tini
    refuses it first: it calls ``tcsetpgrp`` on descriptor 0 before executing
    anything and treats ``EBADF`` as fatal. This runs the script directly, which
    is how the case reaches the guard at all, and the guard keeps it for the
    overridden-entrypoint path.

    Asserting that the server merely started is not enough and was tried: with
    the descriptor passed through untouched it starts perfectly well, and only
    the first read says otherwise. Neither is the name it points at enough, and
    that was tried too: ``/dev/null`` opened for writing reads back as
    ``/dev/null``, so a fallback of ``0>/dev/null`` satisfied every case here
    while handing the server the same unreadable descriptor it was rescued
    from. The access mode is the part that cannot be faked.
    """
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to exercise the Linux image entrypoint")
    major = int(
        subprocess.check_output(
            [bash, "-c", 'printf "%s" "${BASH_VERSINFO[0]}"'], text=True
        )
    )
    if major < 5:
        pytest.skip("wait -n -p requires the Bash 5 shipped by the image")
    if not Path("/proc/self/fdinfo").is_dir():
        pytest.skip("the guard recognises these through /dev/fd and /proc")

    display_number, socket_path, lock_path = _a_free_display(30_000)

    fake_xvfb = tmp_path / "Xvfb"
    fake_xvfb.write_text(_FAKE_XVFB, encoding="utf-8")
    fake_xvfb.chmod(0o755)

    # Reports the descriptor rather than using it. A real server would already
    # be dead in the directory case, which is the point.
    seen = tmp_path / "seen"
    fake_server = tmp_path / "server"
    fake_server.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import fcntl
            import os
            import pathlib

            try:
                target = os.readlink("/proc/self/fd/0")
                mode = fcntl.fcntl(0, fcntl.F_GETFL) & os.O_ACCMODE
                readable = mode in (os.O_RDONLY, os.O_RDWR)
                answer = f"{{target}} readable={{readable}}"
            except OSError as error:
                answer = f"errno={{error.errno}}"
            pathlib.Path({str(seen)!r}).write_text(answer)
            """
        ),
        encoding="utf-8",
    )
    fake_server.chmod(0o755)

    env = {
        **os.environ,
        "DISPLAY": f":{display_number}",
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
    }
    preexec = None
    if kind == "directory":
        handed = os.open(str(tmp_path), os.O_RDONLY)
    elif kind == "write_only":
        handed = os.open(os.devnull, os.O_WRONLY)
    elif kind == "no_access_mode":
        # Linux's fourth access mode grants neither direction. Nothing in
        # Python's `os` names it, and rejecting only write-only let it through.
        handed = os.open(str(tmp_path / "data"), os.O_CREAT | 3)
    elif kind == "path_only":
        # O_PATH names the file without opening it. Every read fails while the
        # access-mode bits still read 0, which is the readable value.
        (tmp_path / "data").write_text("x", encoding="utf-8")
        # `os.O_PATH` is Linux-only and absent from the stubs this file is
        # checked against, so the value is written out. It is the same
        # `010000000` the entrypoint masks against.
        handed = os.open(str(tmp_path / "data"), getattr(os, "O_PATH", 0o10000000))
    else:
        handed = os.open(os.devnull, os.O_RDONLY)
        # Closes descriptor 0 in the child after the fork, which is the state
        # `stdin=subprocess.DEVNULL` cannot produce.
        preexec = lambda: os.close(0)  # noqa: E731

    try:
        process = subprocess.Popen(
            [bash, str(_ENTRYPOINT_PATH), str(fake_server)],
            env=env,
            stdin=handed,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            preexec_fn=preexec,
        )
        try:
            stdout, stderr = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise
    finally:
        os.close(handed)
        socket_path.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)

    assert seen.exists(), (
        "the supervisor never started the server, so an input it does not read "
        f"takes down the container. stdout={stdout!r} stderr={stderr!r}"
    )
    assert seen.read_text(encoding="utf-8") == f"{os.devnull} readable=True", (
        f"the server was handed {seen.read_text(encoding='utf-8')!r} rather "
        f"than a readable {os.devnull}. stderr={stderr!r}"
    )
    assert process.returncode == 0, (stdout, stderr)
