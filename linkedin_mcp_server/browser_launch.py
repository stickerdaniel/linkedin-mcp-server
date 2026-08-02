"""Launch options shared by every browser this server starts.

Two call sites used to build these independently: the runtime path, which also
covers the foreign-runtime bridge and import validation, and the manual login.
A setting added to one silently did not apply to the other, and the login is
the worse one to get wrong — a session created while leaking has been leaking
since the moment it existed.
"""

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from linkedin_mcp_server.config.schema import BrowserConfig

logger = logging.getLogger(__name__)

# WebRTC reaches the network over UDP, which an HTTP proxy does not carry. It
# therefore goes around the proxy and hands the page the machine's real
# address. Measured against a real STUN server: a page saw the proxy's address
# in the HTTP request and the real IPv4 *and* IPv6 in the ICE candidates at the
# same time, which is trivially correlated and leaves the proxy pointless.
#
# Both spellings are passed because each binary reads a different one. Full
# Chrome and Chromium take the plain switch through the command-line pref
# store (`chrome_command_line_pref_store.cc`), and modern `--headless` shares
# that path; the separate `chrome-headless-shell` reads only the `--force-`
# variant, in `headless/lib/browser/headless_web_contents_impl.cc`. Full Chrome
# has no consumer for `--force-` at all. Chromium has no central switch
# registry, so a switch no consumer asks for is simply inert, and the two
# values are identical, so no binary can end up with a conflicting policy.
#
# Verified on all four rungs — real Chrome headed and headless, bundled full
# Chromium, and the headless shell: every baseline produced a server-reflexive
# candidate and every fixed run produced none.
#
# Note this does more than hide the address. With no proxied UDP available, no
# ICE candidate forms at all, so WebRTC stops working rather than lying about
# where it is. That is acceptable here because nothing in this server uses it:
# there is no RTCPeerConnection and no getUserMedia anywhere in the codebase.
_WEBRTC_STAYS_ON_THE_PROXY = (
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
)


def build_launch_options(
    browser: "BrowserConfig",
) -> tuple[dict[str, Any], dict[str, int]]:
    """Build the Patchright launch options and viewport for any browser.

    Takes the configuration rather than reading the global, so the two call
    sites keep resolving it the way they already do and this stays a pure
    function of its input.

    Returns the options dict and the viewport separately because callers pass
    the viewport through a named ``BrowserManager`` argument rather than as a
    launch option.
    """
    viewport = {
        "width": browser.viewport_width,
        "height": browser.viewport_height,
    }

    launch_options: dict[str, Any] = {}

    if browser.chrome_path:
        launch_options["executable_path"] = browser.chrome_path

    proxy = browser.proxy_settings()
    if proxy:
        launch_options["proxy"] = proxy
        # Only where a proxy exists: with a direct connection there is no
        # bypass to prevent, and the switches would disable a working browser
        # capability for nothing.
        launch_options["args"] = list(_WEBRTC_STAYS_ON_THE_PROXY)

    return launch_options, viewport


def describe_launch(launch_options: dict[str, Any]) -> None:
    """Log what the browser was configured with, without the credentials.

    Kept apart from the builder so the two call sites can log at the point
    where their own context makes sense, and so a proxy password cannot reach
    a log by being carried along in a formatted options dict.
    """
    executable = launch_options.get("executable_path")
    if executable:
        logger.info("Using custom Chrome path: %s", executable)

    proxy = launch_options.get("proxy")
    if proxy:
        # The server only. Username and password must not reach the log.
        logger.info("Routing browser traffic through proxy %s", proxy["server"])
        logger.info("WebRTC restricted to the proxy; direct UDP is disabled")
