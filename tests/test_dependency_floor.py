"""The declared patchright floor has to be a version the server can run on.

A dependency floor is not usually worth a test. This one is, because it was
wrong for a long time and nothing anywhere said so: it read ``>=1.40.0``, a
version patchright never published, and it named five minor series below the
one the server actually needs.

Nothing catches that on its own. ``uv`` and ``pip`` resolve to the newest
version satisfying the range, so an ordinary install never exercises the floor
and a false one can sit in the manifest for years. It decides something only in
the environment where another package competes for the same dependency, or
where a resolver is asked for the lowest version on purpose, and that is
precisely the environment nobody runs.

What this file can and cannot do, stated plainly so nobody reads more into it.
It compares a declared number against a number recorded here by hand. That is
a review aid, not a proof: it stops the floor drifting back down, and it
records *why* the floor is where it is, but it cannot discover that a new call
has raised the real minimum. Only resolving to the floor and running against it
can do that, which needs a browser and an installed environment. Treat
:data:`_MINIMUM` as something to raise deliberately when the code starts
calling something newer.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: The oldest patchright the server can actually open a browser with.
#:
#: Set by ``BrowserContext.browser`` being populated on a *persistent* context,
#: which ``hidden_target.open_hidden_page`` requires and refuses to continue
#: without. Through 1.52.5, ``launch_persistent_context`` returned the context
#: alone (``from_channel(await self._channel.send("launchPersistentContext",
#: params))``), so no ``Browser`` object existed and the attribute was ``None``.
#: 1.55.0 switched to ``send_return_as_dict`` and calls
#: ``browser._connect_to_browser_type(...)``, which is what populates it. There
#: is no 1.53 or 1.54 on PyPI, so 1.52.5 is the last version that fails.
#:
#: This matters more than an ordinary missing parameter would, because the
#: hidden-target path fails closed by design: falling back to real headless
#: would restore the very token it exists to remove. On macOS, where that path
#: is the default, an older patchright therefore means no browser at all rather
#: than a degraded one.
#:
#: A lower bound that is no longer the binding one, kept because it is the
#: other version-sensitive call: ``BrowserContext.storage_state(indexed_db=...)``,
#: which the foreign-runtime bridge passes on every checkpoint commit, first
#: appears in 1.51.0. It is reached only once the launch has already succeeded
#: -- ``open_hidden_page`` runs inside ``BrowserManager.start()``, the export
#: well after it -- so on macOS the 1.55 requirement fails first and this one
#: is never got to. On Linux the hidden target is not used at all, so the 1.55
#: bound is not exercised there and this is the one that would bite.
_MINIMUM = Version("1.55.0")


#: Operators that put a floor under the version. ``>`` is a *stricter* bound
#: than ``>=`` and has to count, or this test would fail a change that made the
#: dependency safer.
_LOWER_BOUND_OPERATORS = frozenset({">=", ">", "==", "===", "~="})


def _declared_floor() -> Version:
    """The lower bound ``pyproject.toml`` puts on patchright.

    Parsed with ``packaging`` rather than a regex, because PEP 440 treats
    ``>=1.55`` and ``>=1.55.0`` as the same bound while string or tuple
    comparison does not, and a test that fails on an equivalent spelling is
    worse than no test.

    Two spellings that do bound the floor are still not read: ``==1.55.*`` and
    an arbitrary ``===`` tag. Both would have to be interpreted rather than
    parsed, and neither is a form this project would use. They fall through to
    "no lower bound", which is a wrong-but-loud answer naming the spec that
    caused it, and that is the failure mode to prefer over a crash inside the
    parser.
    """
    pyproject = tomllib.loads(
        (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    for raw in pyproject["project"]["dependencies"]:
        requirement = Requirement(raw)
        if canonicalize_name(requirement.name) != "patchright":
            continue
        bounds = []
        for specifier in requirement.specifier:
            if specifier.operator not in _LOWER_BOUND_OPERATORS:
                continue
            try:
                bounds.append(Version(specifier.version))
            except InvalidVersion:
                # A wildcard (`==1.55.*`) or an arbitrary `===` tag. Skipped
                # rather than allowed to raise: an InvalidVersion escaping here
                # would name a parse bug, and whoever reads the failure needs
                # to be told about the floor instead.
                continue
        assert bounds, f"patchright has no lower bound: {raw!r}"
        return max(bounds)
    raise AssertionError("patchright is not a declared dependency")


def test_the_floor_is_not_below_the_known_minimum() -> None:
    floor = _declared_floor()

    assert floor >= _MINIMUM, (
        f"pyproject declares patchright>={floor}, below the {_MINIMUM} this "
        f"server needs. See _MINIMUM for what breaks: an environment that "
        f"resolves to the floor gets a persistent context with no browser "
        f"object, and the hidden-target launch refuses rather than degrading."
    )
