from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_docker_image_bakes_camoufox_for_runtime_user():
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert "playwright install-deps firefox" in dockerfile

    user_marker = "USER pwuser"
    home_marker = "ENV HOME=/home/pwuser"
    fetch_marker = "from linkedin_mcp_server.bootstrap import _run_camoufox_fetch"
    readiness_marker = "from linkedin_mcp_server.bootstrap import camoufox_ready"
    ownership_marker = "chown -R pwuser:pwuser /home/pwuser/.cache"
    assert dockerfile.index(home_marker) < dockerfile.index(fetch_marker)
    assert dockerfile.index(fetch_marker) < dockerfile.index(ownership_marker)
    assert dockerfile.index(ownership_marker) < dockerfile.index(user_marker)
    assert dockerfile.index(user_marker) < dockerfile.index(readiness_marker)


def test_docker_image_never_bypasses_guarded_camoufox_fetch():
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert "RUN python -m camoufox fetch" not in dockerfile
