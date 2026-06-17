import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
START = REPO_ROOT / "start"


def _run_start(*args: str) -> list[str]:
    env = os.environ | {"LINKEDIN_MCP_DRY_RUN": "1"}
    result = subprocess.run(
        ["zsh", str(START), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return result.stdout.splitlines()


def test_start_defaults_to_local_server() -> None:
    assert _run_start() == ["uv", "run", "-m", "linkedin_mcp_server"]


def test_start_login_shortcut() -> None:
    assert _run_start("login") == ["uv", "run", "-m", "linkedin_mcp_server", "--login"]


def test_start_login_serve_shortcut() -> None:
    assert _run_start("login-serve") == [
        "uv",
        "run",
        "-m",
        "linkedin_mcp_server",
        "--login-serve",
    ]


def test_start_status_shortcut() -> None:
    assert _run_start("status") == ["uv", "run", "-m", "linkedin_mcp_server", "--status"]


def test_start_http_shortcut() -> None:
    assert _run_start("http") == [
        "uv",
        "run",
        "-m",
        "linkedin_mcp_server",
        "--transport",
        "streamable-http",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
        "--path",
        "/mcp",
    ]


def test_start_passes_through_extra_args() -> None:
    assert _run_start("--no-headless") == [
        "uv",
        "run",
        "-m",
        "linkedin_mcp_server",
        "--no-headless",
    ]
