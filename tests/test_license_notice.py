"""The attribution notice has to reach whoever redistributes this.

A licence file is not usually worth a test. This one is, because §4(d) is the
condition that turns attribution into something with a choice of concrete
placements: the notices "contained within such NOTICE file" have to be
reproduced in the derivative's own NOTICE, in its source or documentation, or
in a display where third-party notices normally appear. It binds that
derivative's own derivatives in turn. §4(c) is not weaker in kind, and it names
a place of its own, the Source form of a distributed derivative. It is thinner
here only in what it has to work with: no source file in this repository
carries a copyright header, so the sole notice a fork would be retaining is the
one in the appendix of ``LICENSE``.

All of that hangs on the file being distributed at all, which is a packaging
question rather than a legal one. Rename it, or move to a build backend whose
defaults differ, and the obligation quietly stops existing. Dropping the
``license-files`` entry alone would not do it, because setuptools' own default
patterns cover ``NOTICE*``; that is precisely why the entry is declared, so the
guarantee is a decision rather than an accident of the backend.

Measured once against the real artefacts, on setuptools with
``license-files = ["LICENSE", "NOTICE"]`` declared:

* wheel: ``mcp_server_linkedin-4.23.0.dist-info/licenses/NOTICE``, with
  ``License-File: NOTICE`` in ``METADATA``
* sdist: ``mcp_server_linkedin-4.23.0/NOTICE``

Re-run that with ``uv build`` when the build backend or its version changes.
The checks below are static and cannot see a build; they guard the declaration
that produced those artefacts, and they keep an attribution from reaching
``LICENSE`` without reaching ``NOTICE``. That is a review aid rather than a
proof, the same trade ``test_dependency_floor`` makes and for the same reason.
"""

from __future__ import annotations

from pathlib import Path
import re
import tomllib

_REPO_ROOT = Path(__file__).resolve().parent.parent
_NOTICE = _REPO_ROOT / "NOTICE"
_LICENSE = _REPO_ROOT / "LICENSE"

#: An attribution line, as both files write it. Anchored at the start of the
#: line so the section heading "2. Grant of Copyright License" is not mistaken
#: for one. No name is hard-coded: a second copyright holder should widen what
#: these tests demand rather than needing them edited.
_ATTRIBUTION = re.compile(r"^\s*(Copyright\s+\S.*?)\s*$", re.MULTILINE)


def _attributions(path: Path) -> set[str]:
    return set(_ATTRIBUTION.findall(path.read_text(encoding="utf-8")))


def _license_files() -> list[str]:
    pyproject = tomllib.loads(
        (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    return pyproject["project"]["license-files"]


def test_notice_exists_and_carries_an_attribution() -> None:
    assert _NOTICE.is_file(), (
        "NOTICE is missing; §4(d) has no notices to oblige a fork to reproduce"
    )
    assert _attributions(_NOTICE), (
        "NOTICE carries no copyright line, so reproducing it attributes nothing"
    )


def test_every_licence_attribution_reaches_notice() -> None:
    """A holder named in LICENSE must also be named in NOTICE.

    One direction only. Apache-2.0 does not ask the two files to mirror each
    other, and a NOTICE may legitimately carry attributions the appendix does
    not, so equality would fail on a change that is allowed. What must not
    happen is the reverse: a holder recorded in the appendix and absent from
    the file that §4(d) obliges a fork to reproduce.
    """
    missing = _attributions(_LICENSE) - _attributions(_NOTICE)
    assert not missing, f"named in LICENSE but not in NOTICE: {sorted(missing)}"


def test_both_license_files_are_declared_for_the_distribution() -> None:
    """Declared on purpose, because the default would be an accident.

    setuptools' own globs cover ``NOTICE*`` today, so removing the declaration
    would leave the wheel correct and the guarantee incidental to the backend.
    """
    declared = _license_files()
    assert "LICENSE" in declared, "LICENSE must ship with the distribution"
    assert "NOTICE" in declared, (
        "NOTICE must ship with the distribution, or §4(d) obliges a fork to "
        "reproduce nothing"
    )
