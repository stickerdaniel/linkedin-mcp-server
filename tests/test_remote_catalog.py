"""When the server asks the remote catalog at start, and what the request says.

Every case starts the real request against a local HTTP server and counts what
arrives there, so a gate that stops working shows up as a request that should
not exist. The ``.env`` lookup is pointed at a file per test, because the real
lookup walks up from the package directory and would read whatever ``.env``
the checkout happens to hold.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from catalog_stub import CatalogStub, gate_environment

from linkedin_mcp_server import __version__, remote_catalog
from linkedin_mcp_server.config.loaders import (
    build_parser,
    load_from_args,
    load_from_env,
)
from linkedin_mcp_server.config.schema import AppConfig

_URL = remote_catalog.URL_ENV


@pytest.fixture
def dotenv_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / ".env"
    with gate_environment(monkeypatch, path):
        yield path


@pytest.fixture
def stub(dotenv_file: Path) -> Iterator[CatalogStub]:
    with CatalogStub() as server:
        yield server


def _requests_after_start(stub: CatalogStub, argv: list[str] | None = None) -> int:
    """Start as ``main()`` does and wait out the start budget; count arrivals."""
    remote_catalog.prefetch(argv or [])
    remote_catalog.awaited_body()
    return len(stub.requests)


class TestOptOuts:
    def test_the_override_alone_sends_one_request(self, stub: CatalogStub):
        os.environ[_URL] = stub.url

        assert _requests_after_start(stub) == 1

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("LINKEDIN_MCP_REMOTE_TOOLS", "off"),
            ("LINKEDIN_MCP_REMOTE_TOOLS", " No "),
            ("LINKEDIN_MCP_REMOTE_TOOLS", "0"),
            ("LINKEDIN_MCP_REMOTE_TOOLS", "FALSE"),
            ("LINKEDIN_MCP_CHECK_FOR_UPDATES", "off"),
        ],
    )
    def test_an_opt_out_wins_over_the_override(
        self, stub: CatalogStub, key: str, value: str
    ):
        os.environ[_URL] = stub.url
        os.environ[key] = value

        assert _requests_after_start(stub) == 0

    def test_an_opt_out_in_dotenv_counts(self, stub: CatalogStub, dotenv_file: Path):
        os.environ[_URL] = stub.url
        dotenv_file.write_text("LINKEDIN_MCP_REMOTE_TOOLS=off\n", encoding="utf-8")

        assert _requests_after_start(stub) == 0


class TestCommandLine:
    @pytest.mark.parametrize(
        "argv",
        [
            ["--login"],
            ["--logout"],
            ["--status"],
            ["--sta"],
            ["--help"],
            ["-h"],
            ["--import-from-browser"],
            ["--import-from-browser=chrome"],
            ["--imp", "chrome"],
            ["--daemon"],
            ["--daem"],
        ],
    )
    def test_a_launch_that_never_serves_directly_sends_nothing(
        self, stub: CatalogStub, argv: list[str]
    ):
        os.environ[_URL] = stub.url

        assert _requests_after_start(stub, argv) == 0

    def test_a_daemon_from_the_environment_sends_nothing(self, stub: CatalogStub):
        os.environ[_URL] = stub.url
        os.environ["DAEMON_ENABLED"] = "1"

        assert _requests_after_start(stub) == 0

    @pytest.mark.parametrize("value", ["chrome", "auto", " "])
    def test_an_import_from_the_environment_sends_nothing(
        self, stub: CatalogStub, value: str
    ):
        os.environ[_URL] = stub.url
        os.environ["IMPORT_FROM_BROWSER"] = value

        assert _requests_after_start(stub) == 0

    def test_an_import_from_dotenv_sends_nothing(
        self, stub: CatalogStub, dotenv_file: Path
    ):
        os.environ[_URL] = stub.url
        dotenv_file.write_text("IMPORT_FROM_BROWSER=chrome\n", encoding="utf-8")

        assert _requests_after_start(stub) == 0

    @pytest.mark.parametrize("argv", [["--no-daemon"], ["--no-d"]])
    def test_the_command_line_wins_over_the_daemon_environment(
        self, stub: CatalogStub, argv: list[str]
    ):
        os.environ[_URL] = stub.url
        os.environ["DAEMON_ENABLED"] = "1"

        assert _requests_after_start(stub, argv) == 1

    @pytest.mark.parametrize(
        "argv",
        [["--login-timeout", "5"], ["--login-timeout=5"], ["--log-level", "DEBUG"]],
    )
    def test_an_option_that_only_starts_like_a_command_serves(
        self, stub: CatalogStub, argv: list[str]
    ):
        os.environ[_URL] = stub.url

        assert _requests_after_start(stub, argv) == 1


#: Valid command lines, with the environment they run in. Ones the parser
#: refuses never serve either way and are left out.
_PARITY = [
    ([], {}),
    (["--login"], {}),
    (["--logout"], {}),
    (["--status"], {}),
    (["--sta"], {}),
    (["--stat"], {}),
    (["--logo"], {}),
    (["--help"], {}),
    (["-h"], {}),
    (["--he"], {}),
    (["--import-from-browser"], {}),
    (["--import-from-browser=chrome"], {}),
    (["--import-from-browser", "chrome"], {}),
    (["--imp"], {}),
    (["--daemon"], {}),
    (["--daem"], {}),
    (["--d"], {}),
    (["--no-daemon"], {}),
    (["--no-d"], {}),
    (["--version"], {}),
    (["--version=1"], {}),
    (["--lo"], {}),
    (["--no-daemon", "--bogus"], {}),
    ([], {"DAEMON_ENABLED": "1"}),
    ([], {"DAEMON_ENABLED": " TRUE "}),
    ([], {"DAEMON_ENABLED": "0"}),
    (["--no-daemon"], {"DAEMON_ENABLED": "1"}),
    (["--daemon"], {"DAEMON_ENABLED": "0"}),
    ([], {"IMPORT_FROM_BROWSER": "chrome"}),
    ([], {"IMPORT_FROM_BROWSER": " "}),
    ([], {"IMPORT_FROM_BROWSER": ""}),
    (["--no-daemon"], {"IMPORT_FROM_BROWSER": "chrome"}),
    (["--login-timeout", "5"], {}),
    (["--login-timeout=5"], {}),
    (["--login-inline-wait", "5"], {}),
    (["--login-v"], {}),
    (["--log-level", "DEBUG"], {}),
    (["--transport", "streamable-http"], {}),
    (["--host", "--status x"], {}),
]


def _parser_serves(argv: list[str]) -> bool:
    """What the real loader decides: a server, or a command that ends first."""
    config = load_from_env(AppConfig())
    try:
        load_from_args(config, argv)
    except SystemExit:
        return False
    server = config.server
    return not (
        server.login
        or server.logout
        or server.status
        or server.import_from_browser
        or server.daemon_enabled
    )


class TestParserParity:
    def test_the_early_gate_knows_every_long_option(self):
        parsed = {
            option
            for option in build_parser()._option_string_actions
            if option.startswith("--")
        }

        assert set(remote_catalog.LONG_OPTIONS) == parsed

    @pytest.mark.parametrize(("argv", "environ"), _PARITY)
    def test_the_early_gate_decides_as_the_parser_does(
        self,
        dotenv_file: Path,
        capsys: pytest.CaptureFixture[str],
        argv: list[str],
        environ: dict[str, str],
    ):
        os.environ.update(environ)

        assert remote_catalog.launch_serves(argv) == _parser_serves(argv)


class TestDevelopmentInstalls:
    """``CI`` and a development checkout skip the default URL, never the override."""

    @pytest.fixture
    def default_url(self, stub: CatalogStub, monkeypatch: pytest.MonkeyPatch) -> str:
        monkeypatch.setattr(remote_catalog, "CATALOG_URL", stub.url)
        return stub.url

    def test_this_checkout_is_a_development_checkout(self):
        # The suite runs from a clone, with its git-ignored egg-info on the
        # path; that must not make it look installed.
        assert remote_catalog.is_development_checkout()

    @pytest.mark.parametrize("packages", ["site-packages", "dist-packages"])
    def test_an_installed_copy_is_no_development_checkout(
        self, monkeypatch: pytest.MonkeyPatch, packages: str
    ):
        installed = (
            f"/usr/lib/python3.13/{packages}/linkedin_mcp_server/remote_catalog.py"
        )
        monkeypatch.setattr(remote_catalog, "__file__", installed)

        assert not remote_catalog.is_development_checkout()

    def test_a_development_checkout_sends_nothing(
        self, stub: CatalogStub, default_url: str
    ):
        assert _requests_after_start(stub) == 0

    @pytest.mark.parametrize("kind", ["docker", "mcpb"])
    def test_docker_and_the_bundle_are_no_development_checkout(
        self, stub: CatalogStub, default_url: str, kind: str
    ):
        os.environ["LINKEDIN_MCP_RUNTIME"] = kind

        assert _requests_after_start(stub) == 1

    def test_ci_sends_nothing(self, stub: CatalogStub, default_url: str):
        os.environ["LINKEDIN_MCP_RUNTIME"] = "docker"
        os.environ["CI"] = "true"

        assert _requests_after_start(stub) == 0

    def test_the_override_bypasses_ci_and_the_checkout(self, stub: CatalogStub):
        os.environ[_URL] = stub.url
        os.environ["CI"] = "true"

        assert _requests_after_start(stub) == 1


class TestNeverRaises:
    """``prefetch`` runs before the server is imported; it must never stop it."""

    def test_a_malformed_override_sends_nothing(self, stub: CatalogStub):
        os.environ[_URL] = "http://["

        remote_catalog.prefetch([])

        assert not remote_catalog.requested()

    def test_an_unexpected_failure_sends_nothing(
        self, stub: CatalogStub, monkeypatch: pytest.MonkeyPatch
    ):
        def broken(**_kwargs: object) -> str:
            raise RuntimeError("unexpected")

        os.environ[_URL] = stub.url
        monkeypatch.setattr(remote_catalog, "user_agent", broken)

        assert _requests_after_start(stub) == 0
        assert not remote_catalog.requested()


class TestRedirects:
    def test_a_redirect_is_not_followed(self, stub: CatalogStub):
        with CatalogStub() as elsewhere:
            os.environ[_URL] = stub.url
            stub.answer = stub.redirect(elsewhere.url)

            remote_catalog.prefetch([])

            assert remote_catalog.awaited_body() is None
            assert len(stub.requests) == 1
            assert elsewhere.requests == []


class TestWhatTheRequestSays:
    def _headers(self, stub: CatalogStub) -> dict[str, str]:
        os.environ[_URL] = stub.url
        assert _requests_after_start(stub) == 1
        return stub.requests[0].headers

    def test_the_user_agent_names_version_install_platform_and_python(
        self, stub: CatalogStub
    ):
        os.environ["LINKEDIN_MCP_RUNTIME"] = "docker"

        agent = self._headers(stub)["user-agent"]

        pattern = (
            rf"mcp-server-linkedin/{re.escape(__version__)} "
            r"\(docker; [a-z0-9]+(-[A-Za-z0-9._]+)*-(amd64|arm64|[a-z0-9_]+); "
            r"py3\.\d+\)"
        )
        assert re.fullmatch(pattern, agent), agent

    def test_an_unknown_runtime_is_managed(self, stub: CatalogStub):
        os.environ["LINKEDIN_MCP_RUNTIME"] = "something-else"

        assert "(managed; " in self._headers(stub)["user-agent"]

    @pytest.mark.parametrize(
        ("environ", "expected"),
        [
            ({"LANG": "de_DE.UTF-8"}, "de-DE"),
            ({"LC_ALL": "fr_FR.UTF-8", "LANG": "de_DE.UTF-8"}, "fr-FR"),
            ({"LC_MESSAGES": "pt_BR", "LANG": "de_DE.UTF-8"}, "pt-BR"),
            ({"LANG": "sr_RS@latin"}, "sr-RS"),
        ],
    )
    def test_the_locale_becomes_accept_language(
        self, stub: CatalogStub, environ: dict[str, str], expected: str
    ):
        os.environ.update(environ)

        assert self._headers(stub)["accept-language"] == expected

    @pytest.mark.parametrize("value", ["C", "POSIX", "C.UTF-8"])
    def test_a_c_locale_sends_no_accept_language(self, stub: CatalogStub, value: str):
        os.environ["LANG"] = value

        assert "accept-language" not in self._headers(stub)


@pytest.mark.parametrize(
    ("name", "version", "expected"),
    [
        ("Claude Code", "2.1.0", "Claude Code/2.1.0"),
        ("cliént\n", "1", "clint/1"),
        ("x" * 150, "1", "x" * 100),
        ("only-name", None, "only-name"),
        (None, "1.0", None),
        ("éé", None, None),
    ],
)
def test_the_client_header_is_printable_ascii_and_short(
    name: str | None, version: str | None, expected: str | None
):
    assert remote_catalog.client_header(name, version) == expected
