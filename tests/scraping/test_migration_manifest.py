"""Extractor seam migration inventory contracts."""

from __future__ import annotations

from pathlib import Path

import json
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "tests" / "fixtures" / "scraping-policy" / "migration-manifest.json"
CHECKER = ROOT / "scripts" / "check_scraping_migration_manifest.py"


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def test_manifest_matches_every_current_extractor_seam():
    result = subprocess.run(
        [sys.executable, str(CHECKER), "--check", "--stage", "0"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    current = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert result.returncode == 0, result.stderr
    assert current["extractor_parent"] == ("c5e5e5a6b142e910374b7b26558addfd49ed7f84")
    assert current["seams"]
    assert {
        "string_patch",
        "module_alias",
        "direct_import",
        "private_patch_object",
    } <= {seam["kind"] for seam in current["seams"]}
    assert all(seam["canonical_owner"] for seam in current["seams"])
    assert not {
        "owner-local service",
        "extractor compatibility surface",
        "extractor migration owner",
    } & {seam["canonical_owner"] for seam in current["seams"]}
    assert all(seam["migration_stage"] >= 1 for seam in current["seams"])


def test_manifest_is_canonical_portable_json():
    raw = MANIFEST.read_bytes()
    value = json.loads(raw.decode("utf-8"))

    assert raw.endswith(b"\n")
    assert raw.decode("utf-8") == canonical_json(value)
    assert str(Path.home()) not in raw.decode("utf-8")


def test_checker_rejects_obsolete_seams_at_their_migration_stage():
    result = subprocess.run(
        [sys.executable, str(CHECKER), "--check", "--stage", "1"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "obsolete at stage 1:" in result.stderr
    assert "direct_import" in result.stderr


def test_checkers_offer_no_fixture_update_mode():
    result = subprocess.run(
        [sys.executable, str(CHECKER), "--update"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "unrecognized arguments: --update" in result.stderr
