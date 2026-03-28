"""Security-focused tests for browser profile/cookie persistence."""

import os
import stat
from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.core.browser import BrowserManager, _secure_profile_dirs


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX permission bits are not portable on Windows"
)
def test_secure_profile_dirs_hardens_linkedin_tree(tmp_path):
    root = tmp_path / ".linkedin-mcp"
    profile = root / "profile"

    root.mkdir(mode=0o755)
    profile.mkdir(mode=0o755)

    _secure_profile_dirs(profile)

    assert _mode(root) == 0o700
    assert _mode(profile) == 0o700


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX permission bits are not portable on Windows"
)
def test_secure_profile_dirs_does_not_harden_unrelated_parent(tmp_path):
    parent = tmp_path / "custom"
    profile = parent / "profile"

    parent.mkdir(mode=0o755)
    _secure_profile_dirs(profile)

    assert _mode(parent) == 0o755
    assert _mode(profile) == 0o700


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.name == "nt", reason="POSIX permission bits are not portable on Windows"
)
async def test_export_cookies_writes_owner_only_file(tmp_path):
    manager = BrowserManager(user_data_dir=tmp_path / ".linkedin-mcp" / "profile")
    manager._context = MagicMock()
    manager._context.cookies = AsyncMock(
        return_value=[
            {"name": "li_at", "domain": ".linkedin.com", "value": "secret-value"}
        ]
    )

    cookie_path = tmp_path / ".linkedin-mcp" / "cookies.json"
    ok = await manager.export_cookies(cookie_path)

    assert ok is True
    assert cookie_path.exists()
    assert _mode(cookie_path.parent) == 0o700
    assert _mode(cookie_path) == 0o600
