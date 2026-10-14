# Leave the HTTP row on Windows out of native release gate 1

- Date: 2026-10-14
- Issue: [#606](https://github.com/stickerdaniel/linkedin-mcp-server/issues/606)
- Supersedes: [Scope release gate 1 of the shared-browser daemon](2026-10-14-daemon-gate-1-scope.md), only its list of what is not native in gate 1, which gains one entry
- Status: decided; `daemon_enabled` stays `False` until all four release gates pass

## Decision

The maintainer decided on 2026-10-14 that H-R12-http, a streamable-HTTP server that must stay Direct, is not a native gate 1 cell on Windows. Everything else in the scope record stands.

## Why

A streamable-HTTP server reads no end of input, so the host quits it with an interrupt. Windows delivers that to a child only through a console process group of its own, a topology no other row runs, and building it would cost a harness change and another full evidence dispatch. What it would measure is a configuration branch: an HTTP transport elects no owner, decided before any platform-specific code runs.

## Coverage

The cell stays a counted skip on Windows (`HTTP_NOT_ON_WINDOWS` in `tests/differential/eligibility_rows.py`). The row runs natively on Linux and macOS, and the branch is mapped to its models under "HTTP: no election, on every platform" in `tests/differential/model_coverage.py`, which run on every platform.
