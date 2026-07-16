# Camoufox engine: version pin and the profile-wipe incident

`--browser camoufox` / `BROWSER_ENGINE=camoufox` drives LinkedIn through
[Camoufox](https://github.com/daijro/camoufox) (a stealth-patched Firefox)
instead of Patchright's Chromium. Camoufox is driven through vanilla
`playwright` (a separate pip package from `patchright`), via camoufox's own
`AsyncNewBrowser` helper so its fingerprint-spoofing options apply.

Camoufox owns the complete Firefox fingerprint identity. This project does not
pass imported Chromium user agents or `USER_AGENT` overrides into it: changing
only the UA while retaining Camoufox's Firefox fingerprint is internally
inconsistent and can invalidate a LinkedIn session. Patchright continues to
replay imported Chromium UAs.

## Stable identity across isolated profiles

Camoufox generates a new fingerprint, font/voice subset, history length and
noise seeds on every `launch_options()` call. The server therefore stores a
sanitized `~/.linkedin-mcp/camoufox-identity.json` and binds its SHA-256 digest
to the source login generation. Manual login and every process-isolated scrape
reuse those identity fields through Camoufox's `from_options` path.

The artifact never contains the full process environment, proxy credentials or
profile paths. Runtime policy remains current: humanization, geo/WebRTC data,
locale, timezone, addons, headless mode and the isolated profile path are not
frozen into the identity. A missing, tampered or incompatible artifact fails
closed and requires a new `--browser camoufox --login`; it is never silently
replaced while replaying an existing cookie.

Authentication cookies and the identity artifact are staged privately and
published with `source-state.json` only after live validation and confirmed
browser teardown. Scraping never opens the canonical source browser profile.

## Browser provisioning

The Python package and the patched Firefox binary are separate artifacts.
Selecting Camoufox therefore makes the bootstrap gate check Camoufox's own
package-managed executable. CLI modes such as `--login` fetch it synchronously
through Camoufox's fetch module; normal managed server startup performs the same
fetch in the background. A successful subprocess exit is not enough: the gate
validates the non-empty executable, GeoIP database, and default addon, then
atomically publishes a version-bound completion marker as its final step.
Readiness already fails when repair or cleanup is required, and the marker is
removed before the fetch subprocess starts, so another process cannot launch a
partially downloaded runtime. A fetch is bounded to ten minutes including
cancellation and child-process reaping. Camoufox is capped to the audited 0.4
release line because 0.5 changes all three package/cache layouts.

For manual recovery in a local checkout, run:

```bash
uv run -m linkedin_mcp_server --browser camoufox --status
```

The managed command provisions or repairs Camoufox before checking the LinkedIn
session. Calling the upstream `python -m camoufox fetch` directly does not
publish this project's completion marker and is intentionally not considered a
ready install.

The Docker image installs Playwright's Firefox system dependencies and runs the
same guarded fetch as root with `HOME=/home/pwuser` (the GeoIP downloader also
writes into the image virtualenv), then gives `pwuser` ownership of its user
cache. The build validates the completion marker and runtime assets before
switching users. A container configured with `BROWSER_ENGINE=camoufox` fails
closed if that baked runtime is incomplete instead of continuing to
authentication and failing later.

Importing a Chromium browser session into Camoufox is not supported. A LinkedIn
cookie minted under Chromium belongs to a different complete fingerprint; the
import path fails before discovery and tells the user to create the session with
`--browser camoufox --login`. Automatic import likewise falls through to that
headed login flow.

## The `playwright==1.59.0` pin

`pyproject.toml` pins `playwright==1.59.0` exactly, not the newest version
camoufox's own `playwright<1.61` constraint would otherwise allow. This is
load-bearing, not incidental.

**What happened:** `playwright==1.60.0`'s Firefox glue crashes the entire
Node driver process on an uncaught in-page JS error whose `.location` is
missing (`TypeError: Cannot read properties of undefined (reading 'url')` in
the vendored `coreBundle.js`). Reproduced consistently against a real,
authenticated `linkedin.com/feed/` navigation under Camoufox. `playwright==1.59.0`
does not reproduce it.

**Why it mattered more than "an engine crashed":** the crash happened
mid-navigation during this project's own feed-auth validation
(`drivers/browser.py:_feed_auth_succeeds`), which — before this was fixed —
caught *all* exceptions identically and returned `False`, the same signal as
"LinkedIn rejected this cookie." The caller (`browser_import/orchestrate.py`)
treated that as a confirmed rejection and reset the profile directory. Since
Patchright's and Camoufox's profiles shared one `user_data_dir`, that reset
deleted a working Patchright session that had nothing to do with the failed
Camoufox attempt. Both defects are fixed now: transport failures raise
`NetworkError`, and every candidate is validated in a unique disposable
profile while canonical cookies, identity and source metadata remain untouched.
The version pin removes today's *trigger*; the isolation and transaction rules
remove the *mechanism* that turned a crash into data loss.

## Before bumping `playwright` past 1.59.0

1. Run the smoke test (skipped by default — needs a fetched Camoufox binary
   and, on NixOS, the `LD_LIBRARY_PATH` `run.sh` assembles):
   ```
   LD_LIBRARY_PATH=<gcc-lib>:<gtk/firefox libs> uv run pytest -m camoufox_smoke -v
   ```
   `tests/test_camoufox_smoke.py` drives an uncaught cross-origin JS error
   (the standard way browsers produce a "Script error." with no location —
   the most plausible real trigger, though the exact upstream condition was
   never fully isolated) through the real driver and asserts the connection
   survives.
2. Additionally reproduce an authenticated `/feed/` navigation after creating
   the session manually (`./run.sh --browser camoufox --login`) — the
   smoke test exercises the same *class* of failure, not a guarantee it's
   identical to whatever LinkedIn's page actually threw.
3. Only then bump the pin, and re-run both checks once more against the new
   version before merging.
