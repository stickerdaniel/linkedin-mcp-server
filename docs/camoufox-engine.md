# Camoufox engine: version pin and the profile-wipe incident

`--browser camoufox` / `BROWSER_ENGINE=camoufox` drives LinkedIn through
[Camoufox](https://github.com/daijro/camoufox) (a stealth-patched Firefox)
instead of Patchright's Chromium. Camoufox is driven through vanilla
`playwright` (a separate pip package from `patchright`), via camoufox's own
`AsyncNewBrowser` helper so its fingerprint-spoofing options apply.

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
Camoufox attempt. Both defects are fixed now (transport failures raise
`NetworkError` and are never treated as a rejection; resets are scoped to
the failing engine's own subdirectory and move data aside instead of
deleting it) — see `_is_transport_failure` in `drivers/browser.py` and
`engine_profile_dir` in `core/browser.py`. The version pin removes today's
*trigger*; the code fixes remove the *mechanism* that turned a crash into
data loss.

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
2. Additionally reproduce an authenticated `/feed/` navigation manually
   (`./run.sh --import-from-browser <browser> --browser camoufox`) — the
   smoke test exercises the same *class* of failure, not a guarantee it's
   identical to whatever LinkedIn's page actually threw.
3. Only then bump the pin, and re-run both checks once more against the new
   version before merging.
