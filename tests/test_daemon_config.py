"""Handing an owner the configuration it has to agree on.

The owner is a separate process that opens a browser against a logged-in
session, and it cannot work out its own settings: half of them arrived on the
command line of the *frontend*. Get this wrong in the quiet direction and the
result is not a visible error but a client that elects an owner and then refuses
to use it, because the fingerprint it compares covers exactly these fields.
"""

from __future__ import annotations

import json
from dataclasses import fields

import pytest

from linkedin_mcp_server import daemon_config
from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon_descriptor import config_fingerprint


def _config(**browser: object) -> AppConfig:
    config = AppConfig()
    config.browser.user_data_dir = "~/somewhere/profile"
    for name, value in browser.items():
        setattr(config.browser, name, value)
    return config


class TestRoundTrip:
    def test_the_owner_agrees_with_the_frontend_that_started_it(self):
        # The property that matters, stated the way the daemon uses it: the
        # fingerprint is what a client compares before attaching, so an owner
        # rebuilt from this must produce the same one. Anything dropped in
        # transit shows up here as a client that will not talk to the owner it
        # just started.
        original = _config(
            headless=False,
            user_agent="Mozilla/5.0 (test)",
            proxy_server="http://proxy.example:8080",
            proxy_username="user",
            proxy_password="secret",
            viewport_width=1920,
            login_timeout_seconds=900.0,
        )

        restored = daemon_config.decode(daemon_config.encode(original))

        key = "a-token"
        assert config_fingerprint(restored, key=key) == config_fingerprint(
            original, key=key
        )

    def test_every_browser_setting_crosses_unchanged(self):
        # The fingerprint check above is necessary and not sufficient: it only
        # covers SHARED_CONFIG_FIELDS, and a field left at its default produces
        # the same digest whether it crossed or was dropped. So the whole
        # section is compared field by field, which is what would actually
        # notice a setting quietly going missing from the codec.
        original = _config(
            headless=False,
            slow_mo=25,
            user_agent="Mozilla/5.0 (test)",
            viewport_width=1920,
            viewport_height=1080,
            default_timeout=9000,
            proxy_server="http://proxy.example:8080",
            proxy_username="user",
            proxy_password="secret",
            proxy_bypass="localhost",
            login_timeout_seconds=900.0,
            login_inline_wait_seconds=10.0,
            browser_wait_seconds=30.0,
            browser_min_hold_seconds=5.0,
            browser_idle_timeout_seconds=120.0,
            auto_import_from_browser=False,
            eager_full_chromium=True,
        )

        carried = daemon_config.encode(original)
        restored = daemon_config.decode(carried)

        assert restored.browser == original.browser
        # And every field was actually sent, not merely restored to a default
        # that happened to match. The comparison above cannot tell those apart
        # for any field left at its default — `chrome_path` is one, and so is
        # every field added in future. Checking the serialised keys against the
        # dataclass is what makes the name of this test true.
        assert set(json.loads(carried)["browser"]) == {
            field.name for field in fields(original.browser)
        }

    def test_settings_outside_the_fingerprint_survive_too(self):
        # The fingerprint says which differences stop two clients sharing an
        # owner. It does not say which settings the owner needs to do its job:
        # `browser_wait_seconds` and `browser_min_hold_seconds` are not in
        # SHARED_CONFIG_FIELDS at all, so an owner left on their defaults would
        # hand the browser around on a schedule the user did not choose while
        # every fingerprint still matched. (`browser_idle_timeout_seconds` *is*
        # in that list and is included here only as a third value to carry.)
        #
        # The values chosen survive validate() untouched, so this pins the
        # transport rather than the clamping: the wait has a 45 second ceiling
        # and the minimum hold has to leave room inside it, and picking values
        # those clamps act on would make this a test of the validator.
        original = _config(
            browser_wait_seconds=30.0,
            browser_min_hold_seconds=5.0,
            browser_idle_timeout_seconds=120.0,
        )

        restored = daemon_config.decode(daemon_config.encode(original))

        assert restored.browser.browser_wait_seconds == 30.0
        assert restored.browser.browser_min_hold_seconds == 5.0
        assert restored.browser.browser_idle_timeout_seconds == 120.0

    def test_the_tool_timeout_crosses_because_the_owner_enforces_it(self):
        original = AppConfig()
        original.browser.user_data_dir = "~/p/profile"
        original.server.tool_timeout_seconds = 42.5

        restored = daemon_config.decode(daemon_config.encode(original))

        assert restored.server.tool_timeout_seconds == 42.5


class TestRefusing:
    def test_the_frontends_own_invocation_does_not_cross(self):
        # An owner that adopted the frontend's transport would try to serve
        # stdio, and one that adopted its one-shot flags would re-run the
        # client's --login. Those describe how *that* process was started.
        original = AppConfig()
        original.browser.user_data_dir = "~/p/profile"
        original.server.login = True
        original.server.transport = "streamable-http"
        original.server.port = 9999

        carried = json.loads(daemon_config.encode(original))

        assert "login" not in carried["server"]
        assert "transport" not in carried["server"]
        assert "port" not in carried["server"]

    def test_an_unknown_setting_is_refused_rather_than_skipped(self):
        # Both ends are one installation, so a name this does not recognise did
        # not come from encode(). Skipping it quietly would be an owner running
        # on settings nobody chose.
        with pytest.raises(ValueError, match="unknown"):
            daemon_config.decode(json.dumps({"browser": {"nonsense": 1}, "server": {}}))

    def test_a_frontend_setting_smuggled_into_the_server_section_is_refused(self):
        # The allowed list is enforced on the way in, not only on the way out.
        with pytest.raises(ValueError, match="unknown"):
            daemon_config.decode(json.dumps({"browser": {}, "server": {"login": True}}))

    def test_a_value_of_the_wrong_type_is_refused_before_it_reaches_a_browser(self):
        # JSON cannot tell seconds from a count. Left unchecked this surfaces
        # deep inside a browser launch, in a detached process, as an error
        # nobody can trace back to here.
        with pytest.raises(ValueError, match="wrong type"):
            daemon_config.decode(
                json.dumps({"browser": {"slow_mo": "quickly"}, "server": {}})
            )

    def test_a_boolean_is_not_accepted_where_a_number_belongs(self):
        # bool is a subclass of int, so an isinstance check alone accepts True
        # as a timeout. That is exactly the silent nonsense this refuses.
        with pytest.raises(ValueError, match="wrong type"):
            daemon_config.decode(
                json.dumps({"browser": {"slow_mo": True}, "server": {}})
            )

    def test_an_integer_is_accepted_where_a_float_belongs(self):
        # The other direction, and it has to work: JSON writes 0 for 0.0, so
        # refusing would make an ordinary configuration untransportable.
        restored = daemon_config.decode(
            json.dumps({"browser": {"login_timeout_seconds": 0}, "server": {}})
        )

        assert restored.browser.login_timeout_seconds == 0

    def test_an_optional_setting_may_be_unset(self):
        restored = daemon_config.decode(
            json.dumps({"browser": {"user_agent": None}, "server": {}})
        )

        assert restored.browser.user_agent is None

    def test_a_log_level_outside_the_supported_set_is_refused(self):
        with pytest.raises(ValueError, match="wrong type"):
            daemon_config.decode(
                json.dumps({"browser": {}, "server": {"log_level": "CHATTY"}})
            )

    def test_a_configuration_the_frontend_would_reject_is_rejected_here_too(self):
        # The owner is the process that opens the browser, so a value refused at
        # the frontend must not reach Chromium through this back door.
        #
        # The specific error, not a bare Exception: this is asserting that
        # `validate()` ran, and any decoder bug at all would satisfy a looser
        # check while the validation it names had quietly stopped happening.
        from linkedin_mcp_server.config.schema import ConfigurationError

        with pytest.raises(ConfigurationError, match="tool_timeout_seconds"):
            daemon_config.decode(
                json.dumps({"browser": {}, "server": {"tool_timeout_seconds": -1.0}})
            )

    def test_the_owner_applies_the_browser_mode_it_was_handed(self):
        # Installing the configuration is not enough. The browser mode lives in
        # a module global that defaults to headless, and `_make_browser` reads
        # that global rather than the configuration — the frontend's own entry
        # point sets it explicitly for exactly this reason.
        #
        # Without it, an owner started with `--no-headless` publishes a
        # fingerprint saying so, the frontend compares it and attaches happily,
        # and the browser opens headless anyway: no window, no error, and a user
        # who asked to watch it left wondering.
        #
        # Driven through `main`, and asserted against the global the launcher
        # actually reads. Calling `set_headless` directly here would only prove
        # that setter works; what was broken is that the owner never called it.
        import io
        import sys

        import linkedin_mcp_server.drivers.browser as browser_module
        from linkedin_mcp_server import daemon_owner

        visible = AppConfig()
        visible.browser.user_data_dir = "~/p/profile"
        visible.browser.headless = False

        original = browser_module.current_headless()
        stdin = sys.stdin
        browser_module.set_headless(True)
        sys.stdin = io.StringIO(daemon_config.encode(visible))
        try:
            # It gets as far as taking the lock, which fails on the deliberately
            # invalid descriptor and is reported rather than raised. That is
            # well past the point where the browser mode is settled.
            assert daemon_owner.main(["--lock-fd", "-1"]) == 1

            assert browser_module.current_headless() is False, (
                "the owner ignored the browser mode it was handed"
            )
        finally:
            sys.stdin = stdin
            browser_module.set_headless(original)

    def test_the_owner_refuses_to_start_without_a_configuration(self):
        # The owner reads its settings from standard input and must not fall
        # back to parsing its own command line: `load_config` would then see the
        # daemon's argv, and the browser it opened would be configured by
        # accident rather than by the client that asked for it.
        #
        # Exercised through `main`, which is otherwise reached only inside a
        # spawned process where coverage cannot see it.
        import io
        import sys

        from linkedin_mcp_server import daemon_owner

        original = sys.stdin
        sys.stdin = io.StringIO("   \n")
        try:
            with pytest.raises(ValueError, match="without a configuration"):
                daemon_owner.main([])
        finally:
            sys.stdin = original

    def test_something_that_is_not_a_configuration_is_refused(self):
        with pytest.raises(ValueError, match="not valid JSON"):
            daemon_config.decode("not json at all")

        with pytest.raises(ValueError, match="not an object"):
            daemon_config.decode("[1, 2, 3]")

        with pytest.raises(ValueError, match="no browser section"):
            daemon_config.decode(json.dumps({"server": {}}))
