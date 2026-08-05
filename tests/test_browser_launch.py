"""The shared launch-options builder: which browser, and whose choice.

Both launch paths go through `build_launch_options`, and the reason this file
exists is that the interesting failure is silent. Without a channel, Playwright
picks the *binary* from the `headless` flag alone, so `--login` (which forces
headed) minted every session in the full browser while scraping used the
stripped headless shell. Nothing errors; the two browsers simply differ in
plugins, `window.chrome` and notification permissions.
"""

from __future__ import annotations

from linkedin_mcp_server.browser_launch import build_launch_options
from linkedin_mcp_server.config.schema import BrowserConfig


def test_managed_launch_names_the_channel():
    """A named channel is what makes the binary independent of the mode."""
    options, _ = build_launch_options(BrowserConfig())

    assert options["channel"] == "chromium"


def test_custom_executable_replaces_the_managed_choice():
    """`CHROME_PATH` is an operator decision and wins outright.

    No channel alongside it. Patchright would let the path win anyway, so this
    is about not leaving two competing selectors in one options dict for the
    next reader to reconcile.
    """
    options, _ = build_launch_options(BrowserConfig(chrome_path="/custom/chrome"))

    assert options["executable_path"] == "/custom/chrome"
    assert "channel" not in options


def test_the_choice_does_not_depend_on_headless():
    """The builder must not reintroduce a mode-dependent binary.

    `headless` selects a mode of one browser. If this ever varies by it again,
    sessions get minted in one binary and used in another, which is the defect
    this replaced.
    """
    headless = BrowserConfig(headless=True)
    headed = BrowserConfig(headless=False)

    assert build_launch_options(headless)[0] == build_launch_options(headed)[0]


def test_viewport_comes_from_configuration():
    _, viewport = build_launch_options(
        BrowserConfig(viewport_width=1920, viewport_height=1080)
    )

    assert viewport == {"width": 1920, "height": 1080}


def test_no_proxy_switches_without_a_proxy():
    """The WebRTC switches change an observable capability, so they are
    conditional on there being something to contain."""
    options, _ = build_launch_options(BrowserConfig())

    assert "args" not in options
    assert "proxy" not in options
