"""The image's browser and process topology are part of its behaviour.

Building the real image is too expensive for the ordinary unit suite, and
string checks are enough for the pieces that can disappear silently: the
browser product, the launch mode, and who receives SIGTERM. The supervisor
itself is exercised with fake Xvfb and server processes, because child liveness
is behaviour a string check cannot prove.

Two things resist that treatment and are measured instead. Which paths reach
the build context is decided by `.dockerignore` semantics that no
reimplementation here matched without diverging silently, so a throwaway
`busybox` probe asks the daemon; it costs about a second and skips where there
is no daemon. And a flag is read out of the command that will run it rather
than out of the file, because the file also holds the comment explaining the
flag.
"""

from __future__ import annotations

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

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def _logical_lines(text: str) -> list[str]:
    """Join backslash continuations and drop comments.

    Both the Dockerfile and the compiled constraint file wrap one logical
    statement across several physical lines. Matching a flag against the raw
    text therefore also matches the comment that explains the flag, which is
    how an assertion on ``--no-deps`` stayed green after the flag was removed
    from the command it guards.

    Line continuation is the whole of what this handles, and
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
        statements.append(f"{pending}{stripped}".strip())
        pending = ""
    if pending:
        statements.append(pending.strip())
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


_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOCKERFILE = (_REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
_DOCKERFILE_INSTRUCTIONS = _logical_lines(_DOCKERFILE)
_ENTRYPOINT_PATH = _REPO_ROOT / "docker-entrypoint.sh"
_ENTRYPOINT = _ENTRYPOINT_PATH.read_text(encoding="utf-8")
_README = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
_DOCKER_GUIDE = (_REPO_ROOT / "docs" / "docker-hub.md").read_text(encoding="utf-8")
_BUILD_CONSTRAINTS_PATH = _REPO_ROOT / "build-constraints.txt"
_PYPROJECT_PATH = _REPO_ROOT / "pyproject.toml"
_IMAGE_PYTHON = re.search(r"^FROM python:(\d+)\.(\d+)\.(\d+)-slim", _DOCKERFILE, re.M)
# Pinned: the probe runs against the developer's own machine, and `latest`
# is whatever the registry serves that day.
_PROBE_IMAGE = (
    "busybox@sha256:dc2d74b28e4cf8984fa52af1f39bc7c3d9c73760b41a74d629f5d11b1ab28616"
)


# Sentinels stand in for the real tree. The names on the left have to reach the
# build; the ones on the right must not, and they cover the root, a suffix and
# a nested directory, which is the whole of what `**/.env*` claims.
_CONTEXT_SENTINELS = (
    "build-constraints.txt",
    "build-constraints.in",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "linkedin_mcp_server/__init__.py",
    ".env",
    ".env.example",
    ".env.local",
    ".env.previous-proxy",
    "tests/.env.probe",
    "nested/deeper/.env.secret",
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
        pytest.skip("docker is required to read the real build context")
    # Unavailability is what may skip. Once the daemon answers and the base
    # image is local, a failing probe is a failing probe: treating every
    # non-zero exit as absence turned a malformed `.dockerignore` into a green
    # run that had checked nothing.
    for available in ([docker, "info"], [docker, "pull", "-q", _PROBE_IMAGE]):
        probe = subprocess.run(
            available, capture_output=True, text=True, timeout=300, check=False
        )
        if probe.returncode != 0:
            pytest.skip(f"no usable docker daemon: {probe.stderr.strip()[:200]}")

    context = tmp_path_factory.mktemp("dockerignore-probe")
    shutil.copyfile(_REPO_ROOT / ".dockerignore", context / ".dockerignore")
    for sentinel in _CONTEXT_SENTINELS:
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
    # Any further uv invocation is unaccounted for, whatever it is wrapped in.
    # A second sync would build the backend unconstrained, and a constrained
    # install afterwards cannot undo a build that already ran.
    uv_instructions = [
        instruction
        for instruction in _DOCKERFILE_INSTRUCTIONS
        if "uv sync" in instruction or "uv pip install" in instruction
    ]
    assert uv_instructions == [_DEPENDENCY_SYNC, _PROJECT_INSTALL], uv_instructions
    # `--no-verify-hashes` has an environment twin that reaches every uv
    # invocation in the stage and turns the same verification off.
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
    required = {
        canonicalize_name(Requirement(entry).name) for entry in build_system["requires"]
    }
    assert required

    def named(path: Path) -> dict[str, str]:
        return {
            canonicalize_name(
                Requirement(entry.split("--hash", 1)[0].strip()).name
            ): entry
            for entry in _logical_lines(path.read_text(encoding="utf-8"))
        }

    sources = named(_REPO_ROOT / "build-constraints.in")
    compiled = named(_BUILD_CONSTRAINTS_PATH)

    assert required <= sources.keys(), (
        f"build requirements missing from build-constraints.in: "
        f"{sorted(required - sources.keys())}"
    )
    assert sources.keys() <= compiled.keys(), (
        f"sources missing from the compiled file: "
        f"{sorted(sources.keys() - compiled.keys())}"
    )
    unhashed = sorted(
        name for name, entry in compiled.items() if "--hash=sha256:" not in entry
    )
    assert not unhashed, f"compiled entries without a hash: {unhashed}"

    # A marker that is false where the image builds pins nothing there while
    # reading as a pin here. `platform_machine` is the live one: the release
    # builds amd64 and arm64 from this same file, so an entry true for only one
    # of them leaves the other resolving its backend from the index.
    assert _IMAGE_PYTHON, "the base image no longer names a Python version"
    major, minor, patch = _IMAGE_PYTHON.groups()
    for name, entry in compiled.items():
        marker = Requirement(entry.split("--hash", 1)[0].strip()).marker
        if marker is None:
            continue
        for machine in ("x86_64", "aarch64"):
            # Every key `default_environment()` defines has to be named here.
            # `evaluate` overlays this mapping onto the host's, so anything
            # omitted is answered by the machine running the test: the host is
            # on 3.13.15 while the image pins 3.13.13, and a marker reading
            # `implementation_version != "3.13.13"` evaluated true here and
            # false in the image. The kernel-dependent pair is left empty
            # rather than guessed, so a marker touching it fails loudly.
            environment = {
                "implementation_name": "cpython",
                "implementation_version": f"{major}.{minor}.{patch}",
                "os_name": "posix",
                "platform_machine": machine,
                "platform_python_implementation": "CPython",
                "platform_release": "",
                "platform_system": "Linux",
                "platform_version": "",
                "python_full_version": f"{major}.{minor}.{patch}",
                "python_version": f"{major}.{minor}",
                "sys_platform": "linux",
            }
            assert marker.evaluate(environment), (
                f"{name} is constrained only outside the image: {marker} on {machine}"
            )


def test_the_hashed_build_constraint_reaches_the_build_context(
    build_context: frozenset[str],
) -> None:
    """An excluded constraint file drops a pin the build never asked for."""
    needed = [
        name for name in _CONTEXT_SENTINELS if not Path(name).name.startswith(".env")
    ]
    missing = sorted(name for name in needed if name not in build_context)
    assert not missing, f"the build needs these and .dockerignore drops them: {missing}"


def test_the_build_context_carries_no_environment_file(
    build_context: frozenset[str],
) -> None:
    """A suffixed `.env` carries the same secrets as the plain one.

    `COPY . .` keeps the whole context in the builder layer, and the release
    workflow exports that layer through `cache-to: type=gha,mode=max`. Ignoring
    only `.env` let a local `.env.previous-proxy` through, holding a LinkedIn
    session cookie and proxy credentials; a real build listed it inside `/app`.
    The runtime stage takes only `/app/.venv`, so no published image carried it.
    """
    # The fixture writes one of each shape, so an empty result is a real
    # exclusion rather than an empty question.
    offered = [
        name for name in _CONTEXT_SENTINELS if Path(name).name.startswith(".env")
    ]
    assert len(offered) >= 4, offered
    leaked = sorted(
        path for path in build_context if Path(path).name.startswith(".env")
    )
    assert not leaked, f"environment files reached the build context: {leaked}"


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
        assert "created by the Docker viewer" in document
        assert "belongs to a foreign runtime" in document


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

    display_number = 10_000 + (os.getpid() % 10_000)
    display = f":{display_number}.0"
    socket_path = Path(f"/tmp/.X11-unix/X{display_number}")
    lock_path = Path(f"/tmp/.X{display_number}-lock")
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.unlink(missing_ok=True)
    lock_path.unlink(missing_ok=True)
    stale_socket = socket.socket(socket.AF_UNIX)
    stale_socket.bind(str(socket_path))
    stale_socket.close()
    lock_path.write_text("stale", encoding="utf-8")

    fake_xvfb = tmp_path / "Xvfb"
    fake_xvfb.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import pathlib
            import socket
            import sys
            import time

            number = sys.argv[1].removeprefix(":").split(".", 1)[0]
            path = pathlib.Path(f"/tmp/.X11-unix/X{number}")
            lock = pathlib.Path(f"/tmp/.X{number}-lock")
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() or lock.exists():
                raise SystemExit(91)
            server = socket.socket(socket.AF_UNIX)
            server.bind(str(path))
            server.listen()
            lock.write_text("owned", encoding="utf-8")
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
    fake_server = tmp_path / "server"
    fake_server.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import os
            import pathlib
            import signal
            import time

            marker = pathlib.Path(os.environ["SERVER_TERM_MARKER"])

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

    assert process.returncode != 0, (stdout, stderr)
    assert term_marker.read_text(encoding="utf-8") == "term"
