# Browser fingerprint verification

How to check that the browser this server drives still presents one coherent
identity, and what has been measured so far.

The rules live in `AGENTS.md`. This file is the reference behind them: which
tools to run, what each one alone would miss, and the numbers to compare
against.

## The bar

**Coherence, not invisibility.**

Invisibility cannot be demonstrated — no measurement proves a detector did not
notice. A contradiction can be demonstrated, by anyone, at any time. So the
standard is that nothing the browser says about itself is refuted by another
surface of the same browser.

Concretely, all of the following must hold:

- page, dedicated worker, service worker and cross-origin iframe report the
  same user-agent, and their request headers match what their JavaScript says
- the UA major version equals the `sec-ch-ua` brand major version
- `architecture`, `bitness` and `fullVersionList` are non-empty
  (`platformVersion` is normatively empty on Linux)
- no `HeadlessChrome` on any surface
- `navigator.webdriver` is `false` and its property descriptor is unremarkable
- no `__pwInitScripts`, `__playwright__`, `$cdc_*` or similar globals
- the outer window is not larger than the reported screen
- with a proxy configured, no server-reflexive ICE candidate appears

## The tools

Four, because each sees a layer the others cannot. Any one alone gives false
confidence.

| Tool | Covers | What it alone would miss |
|---|---|---|
| [CreepJS](https://github.com/abrahamjuliot/creepjs) | Broadest JS surface; flags self-contradictions as "lies" | Network layer, and current automation-library artefacts |
| [fpscanner](https://github.com/antoinevastel/fpscanner) | Automation and CDP signals, worker/iframe coherence | Network layer; renewed in 2026, so it knows current Playwright tells |
| [rebrowser-bot-detector](https://github.com/rebrowser/rebrowser-bot-detector) | `Runtime.enable`, `__pwInitScripts`, default viewport, Chrome-for-Testing UA | Everything CreepJS covers |
| [TrackMe](https://github.com/pagpeter/TrackMe) / [Fingerproxy](https://github.com/wi1dcard/fingerproxy) | JA3, JA4, HTTP/2 Akamai fingerprint, header order | Anything visible to JavaScript |

Hosted equivalents exist (`tls.peet.ws/api/all` returns the network layer as
JSON) but send the fingerprint to a third party. Clone locally under `.debug/`
where that matters.

Loopback is a secure context, so a local `http://127.0.0.1` server is enough
for service workers and the other secure-context APIs. No self-signed
certificate, no `ignore_https_errors` — both would perturb what is being
measured.

Read the values from a `<script>` in the page itself, not through
`page.evaluate()`. Patchright evaluates in an isolated world, which is not what
a website sees.

## Not covered by any of them

**Behaviour.** Mouse paths, typing rhythm, scroll cadence, the spacing between
navigations across a session. No static page can measure it, and for a platform
like LinkedIn it is plausibly weighted higher than anything above. Out of scope
here; noted so nobody mistakes a clean sweep for a clean bill of health.

Of roughly 112 observable categories catalogued, about twenty have been
measured. The largest untested groups are realm consistency beyond the UA,
codec and DRM support, and Chrome-versus-Chromium feature detection.

## Measured

macOS 26.6 arm64, patchright 1.60.1, Google Chrome 150.0.7871.187, bundled
Chrome for Testing 148.0.7778.96. CreepJS scores are "headless" / "like
headless".

| Configuration | CreepJS | Notes |
|---|---|---|
| Real Chrome, headed, no override | 0% / 44% | Genuine `"Google Chrome"` brand, `sec-ch-ua-arch: arm`, DPR 2 |
| Bundled Chromium, headless, UA claiming 143 | 33% / 88% | `hasMissingChromeObject` high severity; UA and hints disagree |
| Full Chromium headless (`channel="chromium"`) | 67% / 50% | Two high-severity fpscanner rules from the headless token |
| Headless shell (the old default) | — | `plugins.length = 0`, no `window.chrome`, notification permission incoherent |
| Hidden target, windowless mode (macOS) | — | No headless token in UA or brands; `visible` / focused; rAF at 100% of a control window |
| Docker, headed under Xvfb | 0% / 44% | No headless token, native hints; Xvfb is started directly in Python's process group |
| Docker, Xvfb + Mesa llvmpipe | 0% / 44% | WebGL1 and WebGL2; byte-identical renderer across both published architectures |

The windowless mode, measured end to end through `BrowserManager`:

| | Value |
|---|---|
| User agent | `…Chrome/148.0.0.0…`, no `HeadlessChrome` |
| `sec-ch-ua` brands | `Not/A)Brand`, `Chromium` |
| `navigator.webdriver` | `false` |
| `document.visibilityState` / `hasFocus()` | `visible` / `true` |
| `requestAnimationFrame` | 122/s against a control visible window at 122/s |
| Cookie across a full restart | survives |
| Window on screen once settled | none |
| Window on screen during startup | ~550 ms, median of five runs (504-593) |

The rAF figure is the one that mattered: hiding the application at OS level
throttled it to about 1 Hz against 120, which is what disqualified that
approach. A hidden target runs at the same rate as an ordinary window.

**It applies to macOS only, and that is a measured limit rather than a
scoping decision.** The mechanism needs the browser to survive losing its last
visible window, because removing that window is the whole point. Measured in the
published container image, under Xvfb: closing the startup page kills Chromium
and the hidden page dies with it, while keeping that page open leaves everything
working. Without a display a headed launch does not start at all
(`TargetClosedError`). macOS does not quit an application when its last window
closes, which is why it works there. Windows is untested and plausibly behaves
like Linux, so it is not claimed.

Linux is less a gap than a different answer: under a virtual display nobody is
looking at the screen, so an ordinary window is already invisible and there is
nothing to hide.

### Docker WebGL on the virtual display

Measured in the candidate image on Linux amd64 and arm64, ten cold browser
launches per architecture. Headed Chromium under Xvfb started with no WebGL1 or
WebGL2 context even though Mesa's `swrast_dri.so` and `kms_swrast_dri.so` were
present. The result was 0/10 on both architectures, and every explicit renderer
selector tried stayed there: `--use-gl=angle --use-angle=gl`, `--use-gl=egl`,
`--use-gl=desktop` and ANGLE Vulkan.

The working configuration is the simpler one:

```
--enable-webgl --ignore-gpu-blocklist
```

It does not choose a renderer. It lets Chromium use the Mesa path already in the
image. Results, across all twenty cold launches:

| | amd64 | arm64 |
|---|---|---|
| WebGL1 context | 10/10 | 10/10 |
| WebGL2 context | 10/10 | 10/10 |
| Unmasked renderer | `ANGLE (Mesa/X.org, llvmpipe (LLVM 15.0.6 128 bits), OpenGL 4.5)` | same, byte-identical |
| SwiftShader | absent | absent |
| GPU-process crash | none | none |
| High-entropy architecture / bitness | `x86` / `64` | `arm` / `64` |
| Headless token | absent | absent |
| Page, workers and cross-origin frame agree | yes | yes |
| `outer <= screen` | yes | yes |

SwiftShader was measured as the one other path that creates both contexts. It is
excluded: its renderer names `SwiftShader` explicitly, and Patchright strips the
unsafe fallback on purpose because that string is an automation signal in its
own right. `LIBGL_ALWAYS_SOFTWARE=1` changed none of the failed explicit-selector
results and is not set.

The display adds no package of its own: Xvfb already comes from Patchright's
Chromium dependency set, and starting it directly needs no `xauth`. Installing
only full Chromium with `--no-shell` makes the resulting image smaller than the
previous full-plus-shell image, not larger. Measured from clean builds of the
same source: 543.5 to 429.6 MiB on arm64 (-113.9 MiB), and 512.4 to 406.2 MiB on
amd64 (-106.2 MiB), using Docker's uncompressed image size.

### Docker shutdown and display lifetime

`xvfb-run` is not the image supervisor. Measured under `docker stop`,
`tini -g -- xvfb-run ... python` exited 143 without Python running its shutdown
path: `xvfb-run` has an EXIT trap for Xvfb and no TERM forwarder for its child.
Starting Xvfb and Python under one small supervisor in the same process group
lets `tini -g` deliver TERM to all three. The supervisor also makes display
liveness container liveness: if Xvfb dies, it terminates Python and exits
non-zero instead of leaving a live MCP endpoint whose next browser cannot open.
Uvicorn logged its complete application shutdown before the container exited,
in 0.50 seconds.

The experimental daemon is refused in a container. Its owner deliberately
starts a new session so it can outlive a stdio frontend; measured there, it had
a distinct process group while Xvfb remained the frontend's child. Letting that
owner survive would give the browser a lifetime the display does not share.

Two things this does not claim. The half second of visible window on every
browser start cannot be shortened from here — roughly 250 ms passes before
`launch_persistent_context()` returns and about 340 ms is macOS tearing the
window down, leaving about 90 ms that is ours. And the windowless page reports
`outerWidth == screen.width`, which no real window does, since a real one has
chrome and sits inside its display. That is unchanged from the previous
headless default rather than introduced here, and it stays on the list of
things worth fixing.

Window geometry, read from a page's own `<script>` on a loopback origin:

| Configuration | Outer | Screen | DPR | Window fits its screen |
|---|---|---|---|---|
| Headless, explicit viewport | 1280x720 | 1280x720 | 1 | yes |
| Headed, `no_viewport=True` | 1200x958 | 1728x1117 | 2 | yes |
| Headed with an emulated viewport (before) | 1280x805 | 1280x720 | 1 | **no** |

The last row is the contradiction this was measured to remove: an outer window
taller than the screen the same browser reported standing on. Any page can read
both and compare them. Note the headed row now shows the real display and a
Retina DPR of 2, which is an ordinary Mac rather than a shape nothing sells.

Headless keeps an explicit viewport deliberately: headless plus `no_viewport`
collapses the screen to 800x600.

Network layer, same machine:

| | Real Chrome 150 | Bundled Chromium 148 |
|---|---|---|
| JA4 | `t13d1517h2_8daaf6152771_…` | `t13d15**16**h2_8daaf6152771_…` |
| HTTP/2 Akamai | `1:65536;2:0;4:6291456;6:262144\|15663105\|0\|m,a,s,p` | identical |

The TLS handshakes differ by one extension. That is invisible to every
JavaScript test above and cannot be influenced by any launch option.

### Re-measured on Chrome for Testing 149

The windowless-mode table near the top of this section is stamped at patchright
1.60.1. When the lock moved to 1.61.2 (macOS bundles Chrome for Testing
149.0.7827.55; the arm64 Linux image bundles Playwright's own Chromium build at
the same revision, which is why the product names differ by platform), those
identity properties were taken again through `BrowserManager` on macOS.

Against a loopback origin rather than `about:blank`: `navigator.userAgentData`
needs a secure context, and `about:blank` is not one, so a probe there measures
nothing. Verified rather than assumed — `about:blank` reports
`isSecureContext: false` and `userAgentData: undefined`, loopback reports
`true` and an object.

| | Value on 149 |
|---|---|
| User agent | `…Chrome/149.0.0.0…`, no `HeadlessChrome` |
| `sec-ch-ua` brands | `Chromium/149`, `Not)A;Brand/24`, major agreeing with the UA |
| High-entropy hints | `arm`, `64`, two `fullVersionList` entries |
| `navigator.webdriver` | `false` |
| `document.visibilityState` / `hasFocus()` | `visible` / `true` |
| Outer window vs screen | 1280x720 on 1280x720, so it fits, but see below |
| `navigator.plugins.length` / `window.chrome` | 5 / `object` |
| `Notification.permission` | `default` |
| `requestAnimationFrame` | 122/s, matching the 148 figure |

The geometry row is not a clean result. `outerWidth == screen.width` is the
open item recorded above: it fits, which is all the coherence bar asks, but no
real window reports it, and 149 did not change that either way.

Not re-measured on 149: the CreepJS and fpscanner scores, the JA4 and HTTP/2
fingerprints, the startup-flash timing, and the cookie-across-restart check.
Those rows stay 148 measurements and are labelled as such rather than assumed
to carry over.

"Window on screen once settled" is not in that list and not in the table
either, because it is implied rather than skipped: the windowless path fails
closed, so a run that produced these values had a hidden target and no window.
That is an inference, not a CoreGraphics poll like the 148 row was.

## Things that look like fixes and are not

- **`--user-agent` as a browser switch.** Reaches every target including
  service workers, but empties `architecture`, `bitness`, `platformVersion` and
  `fullVersionList` everywhere. A browser answering `Accept-CH` with blanks is
  rarer than one admitting it is headless.
- **A context-level `user_agent`.** Changes the string, leaves the hints, never
  reaches service workers. On Apple Silicon it also flips `sec-ch-ua-arch` to
  `x86` while WebGL still reports an Apple GPU.
- **`--force-webrtc-ip-handling-policy` on its own.** Read only by
  `chrome-headless-shell`; full Chrome has no consumer for it. The plain
  spelling is the one full Chrome reads. Both are passed for that reason.
- **`--host-resolver-rules=MAP * ~NOTFOUND` without an exclusion.** Fails every
  navigation with `ERR_PROXY_CONNECTION_FAILED`, including when the proxy is a
  bare IP. The proxy host always needs excluding.
- **A patched Chromium fork.** Would genuinely fix the service-worker case, at
  the cost of a per-platform build, signing, notarisation and a rebase every
  two weeks. Xvfb reaches the same place without a fork.
