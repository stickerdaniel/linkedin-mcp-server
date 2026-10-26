# Browser session import: supported browsers

`--import-from-browser` reads and decrypts the LinkedIn session cookie from a
locally installed Chromium-based browser. Each
browser needs three things correct: the on-disk user-data path, the Cookies
database location, and the OS keystore label used to decrypt the cookie value.

These cannot be assumed. Some Chromium forks rename the macOS Keychain item:
Helium stores its key under the service `Helium Storage Key`, not the usual
`<name> Safe Storage`. A mocked unit test passes whether or not the label is
right, so every label below is checked against a real browser or an
authoritative source.

The macOS read now queries by **account first** (`security find-generic-password
-a <account> -w`). The account stays the bare product name even when a fork
renames the service, so it is the fork-invariant key: Helium's account is
`Helium` while its service is `Helium Storage Key`. The precise account+service
pair (`-a <account> -s "<account> Safe Storage"`) is the fallback when the
account-only match is absent. This was cross-checked against yt-dlp
(`_get_mac_keyring_password`, which queries the account+service pair) and
HackBrowserData (`browser/browser_darwin.go` `KeychainLabel`).

## Support matrix (macOS)

| Browser | Key | User-data subdir | Keychain account | Verified |
| --- | --- | --- | --- | --- |
| Google Chrome | `chrome` | `Google/Chrome` | `Chrome` | live |
| Chromium | `chromium` | `Chromium` | `Chromium` | live |
| Brave | `brave` | `BraveSoftware/Brave-Browser` | `Brave` | live |
| Arc | `arc` | `Arc/User Data` | `Arc` | live |
| Helium | `helium` | `net.imput.helium` | `Helium` (service `Helium Storage Key`) | live |
| Microsoft Edge | `edge` | `Microsoft Edge` | `Microsoft Edge` | source |
| Vivaldi | `vivaldi` | `Vivaldi` | `Vivaldi` | source |
| Yandex | `yandex` | `Yandex/YandexBrowser` | `Yandex` | source |
| Naver Whale | `whale` | `Naver/Whale` | `Whale` | source |
| Cốc Cốc | `coccoc` | `Coccoc` | `CocCoc` | source |
| Opera | `opera` | `com.operasoftware.Opera` | `Opera` (layout `flat`) | source |
| Opera GX | `opera_gx` | `com.operasoftware.OperaGX` | `Opera` (layout `flat`) | source |

- **live**: cookie decryption tested against the real browser on a development
  machine (the account-first keychain key actually decrypts a stored cookie).
- **source**: not installed on the dev machine; the path and keychain account are
  cross-checked against yt-dlp (`yt_dlp/cookies.py`) and HackBrowserData
  (`browser/browser_darwin.go`), which both decrypt these browsers in production.

The keychain column lists the **account** (`-a`), the primary lookup key. The
service is `<account> Safe Storage` for every browser except Helium, which
renames the service to `Helium Storage Key` (the account stays `Helium`).

The CocCoc user-data directory leaf is `Coccoc` (lowercase c's) but its keychain
account is `CocCoc` (camel case); the casing split is deliberate. The macOS
account (`CocCoc`), `mac_subpath` (`Coccoc`), and the Windows path
(`CocCoc/Browser/User Data`) were each re-confirmed against HackBrowserData
`browser_darwin.go` + `browser_windows.go`. CocCoc is not installed on the dev
machine, so the values are verified against the authoritative source but not yet
live.

Linux and Windows paths follow the same Chromium conventions and the same
sources; the Linux Secret Service application token lives in the
`SUPPORTED_BROWSERS` registry (`browser_import/discovery.py`, the
`linux_app_token` field), not a hardcoded map in `extract.py`.

## Verification checklist for a browser

Before adding or trusting a browser, confirm each line:

- [ ] User-data root resolves on the target OS and contains a `Local State` file.
- [ ] Cookies DB is found (`Default/Network/Cookies` preferred, else `Default/Cookies`).
- [ ] `li_at` metadata reads keychain-free (expiry and last-access from the SQLite columns).
- [ ] The macOS Keychain **service name decrypts a real cookie** (the host-key
      digest matches), not merely "the keychain item exists". Forks rename it.
- [ ] End-to-end: signed into LinkedIn in that browser, `--import-from-browser <key>`
      decrypts and validates against `/feed/`.

The fourth line is the one that bites: confirm the service against a real
browser or against yt-dlp / HackBrowserData, never a guess.

## User agent of an imported session

There is none. An imported cookie is replayed under the runtime browser's own
user agent, and nothing about the source browser's is carried forward.

The import used to synthesize the source browser's UA, on the reasoning that
LinkedIn ties a session token to the fingerprint it was minted under. That was
never measured, and the mechanism did not do what it claimed. Patchright
overrides only the UA string, so the client hints kept reporting the real
browser and any page could see the two disagree; the override never reached
service workers at all
([playwright#5237](https://github.com/microsoft/playwright/issues/5237), closed
as an upstream Chromium bug); and the value was frozen at import time while the
browser it described kept updating.

A browser reporting its own identity consistently beats one caught contradicting
itself, so the synthesis, the `user_agent` key in `source-state.json`, and the
`USER_AGENT` / `--user-agent` settings are all gone. The settings are refused at
startup rather than ignored. See `docs/browser-fingerprint.md` for the
measurements.

## Flat layout (Opera)

Opera and Opera GX keep `Local State` at the user-data root (so the install gate
`_has_local_state` works unchanged) but store cookies at that root with no
`Default/` subdir. The `layout="flat"` registry field drives `enumerate_profiles`
to treat the root itself as the single profile (`profile_dir_name "."`, so
`root / "." == root`), and `resolve_cookies_db` finds `Network/Cookies` or
`Cookies` there. Nothing branches on the browser name: the only `layout`-keyed
branch is in `enumerate_profiles`. Both share the macOS keychain account `Opera`,
and on Windows they live under `%APPDATA%` (Roaming), which the base-dir search
already covers.

## Not yet supported

Epic is not yet supported. Add a browser by verifying its path and keychain
account with the checklist above.
