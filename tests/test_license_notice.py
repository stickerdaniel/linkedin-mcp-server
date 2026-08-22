"""The attribution notice has to reach whoever redistributes this.

A licence file is not usually worth a test. This one is, because the obligation
it creates is the only one in Apache-2.0 that names a *place*. Section 4(d)
binds a derivative work to reproduce the notices "contained within such NOTICE
file" somewhere a reader finds them, and it binds that derivative's own
derivatives in turn. Sections 4(a) and 4(c) are weaker here by construction: no
source file in this repository carries a copyright header, so the only notice a
fork could retain lives in the appendix of ``LICENSE``.

What that hangs on is whether the file is distributed at all, and that is a
packaging question rather than a legal one. ``license-files`` is what carries it
into the wheel and the sdist; drop the entry, rename the file, or move to a
build backend with different defaults, and the obligation quietly stops
existing. Nothing else in the suite would notice, because nothing imports it and
no runtime path reads it.

Measured once against the real artefacts, on setuptools with
``license-files = ["LICENSE", "NOTICE"]`` declared:

* wheel: ``mcp_server_linkedin-4.23.0.dist-info/licenses/NOTICE``, with
  ``License-File: NOTICE`` in ``METADATA``
* sdist: ``mcp_server_linkedin-4.23.0/NOTICE``

Re-run that with ``uv build`` when the build backend or its version changes.
The checks below are static and cannot see a build; they guard the declaration
that produced those artefacts, and they keep the two copyright lines from
drifting apart. That is a review aid rather than a proof, the same trade
``test_dependency_floor`` makes and for the same reason.
"""

from __future__ import annotations

from pathlib import Path
import tomllib

_REPO_ROOT = Path(__file__).resolve().parent.parent
_NOTICE = _REPO_ROOT / "NOTICE"
_LICENSE = _REPO_ROOT / "LICENSE"

#: The one attribution notice this project publishes. It appears in the
#: appendix of ``LICENSE`` and again in ``NOTICE``; a fork that keeps either is
#: keeping this line.
_COPYRIGHT = "Copyright 2025 Daniel Sticker"


def _license_files() -> list[str]:
    pyproject = tomllib.loads(
        (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    return pyproject["project"]["license-files"]


def test_notice_exists_and_carries_the_copyright() -> None:
    assert _NOTICE.is_file(), (
        "NOTICE is missing; Apache-2.0 §4(d) binds nobody without it"
    )
    assert _COPYRIGHT in _NOTICE.read_text(encoding="utf-8"), (
        f"NOTICE must carry {_COPYRIGHT!r}, which is what a derivative work reproduces"
    )


def test_license_appendix_agrees_with_notice() -> None:
    """The two copies of the notice must not drift apart.

    A fork reading ``LICENSE`` and a fork reading ``NOTICE`` have to come away
    with the same attribution, or §4(c) and §4(d) ask for different things.
    """
    assert _COPYRIGHT in _LICENSE.read_text(encoding="utf-8"), (
        f"LICENSE no longer carries {_COPYRIGHT!r}; NOTICE and the appendix disagree"
    )


def test_both_license_files_are_declared_for_the_distribution() -> None:
    """Undeclared is the failure mode, because it is silent.

    setuptools' default globs happen to cover ``NOTICE*`` today, so removing the
    declaration would leave the wheel correct and the guarantee accidental.
    """
    declared = _license_files()
    assert "LICENSE" in declared, "LICENSE must ship with the distribution"
    assert "NOTICE" in declared, (
        "NOTICE must ship with the distribution, or the §4(d) obligation "
        "applies to nothing"
    )
