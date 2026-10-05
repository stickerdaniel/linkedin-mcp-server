"""Stage a signed-in session with the frozen baseline's own code.

Run by the baseline venv's interpreter, never imported by the harness:
``<baseline python> baseline_stage.py PROFILE [DIAGNOSTICS]``, with the row's
environment. The harness has already written the synthetic cookie file and
told the origin its session. This does the rest of what
``session.stage_signed_in_session`` does, with the baseline's import
validation and its browser: record the finished browser install, validate the
cookie file, commit the source state.

Only the harness's ``tests`` directory is added to the path, for
``differential.session`` and ``differential.first_navigation``; the repository
root is not, so every ``linkedin_mcp_server`` import resolves to the
baseline's installed package. With *DIAGNOSTICS*, the baseline's browser is
observed from before its launch (``first_navigation.observe_navigation``),
which listens and asks the browser nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path

# In place of this script's own directory, whose module names (``events``,
# ``session``) would otherwise shadow any top-level module of the same name.
sys.path[0] = str(Path(__file__).resolve().parents[1])

from differential.first_navigation import observe_navigation  # noqa: E402
from differential.session import StagingError, stage_installed_browser  # noqa: E402
from linkedin_mcp_server.drivers.browser import (  # noqa: E402
    get_profile_dir,
    validate_imported_cookies,
)
from linkedin_mcp_server.session_state import (  # noqa: E402
    canonical,
    portable_cookie_path,
    write_source_state,
)


def main(profile: Path, diagnostics: Path | None = None) -> int:
    with (
        observe_navigation(diagnostics, label="frozen-staging")
        if diagnostics is not None
        else contextlib.nullcontext()
    ):
        if canonical(get_profile_dir()) != canonical(profile):
            raise StagingError(
                f"the baseline is configured for {get_profile_dir()}, not {profile}"
            )
        stage_installed_browser()
        if not asyncio.run(
            validate_imported_cookies(portable_cookie_path(profile), profile)
        ):
            raise StagingError(
                "the baseline's own import validation rejected the synthetic session"
            )
        write_source_state(profile)
    return 0


if __name__ == "__main__":
    sys.exit(
        main(
            Path(sys.argv[1]),
            Path(sys.argv[2]) if len(sys.argv) > 2 else None,
        )
    )
